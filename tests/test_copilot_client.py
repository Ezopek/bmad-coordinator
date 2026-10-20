"""GitHub Copilot CLI transport (plan Task 4, design section 14): author-only argv and isolation, the file-written
return, session freshness sealed once, the terminal rule, client facts, briefs and doctor. Every launch runs the fake
`copilot`; nothing here is evidence about the live CLI."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coordinator_core import bmad_planning as planning
from coordinator_core import bmad_review as review
from coordinator_core import clients, worker
from coordinator_core.run_receipt import canonical
from coordinator_core.worker import historical_return, inspect_run, reconcile_run
from tests.fake_clients import FORBIDDEN, Harness, golden_normalize, safe_path
from tests.test_bmad_core import writes
from tests.test_bmad_planning import ARCH, SCOPE, WS, flow

GOLDEN = Path(__file__).parent / "fixtures" / "codex-322752a"
COPILOT = "copilot"
SESSION = "5b0c7e3a-9d41-4f2e-8a63-1c2d3e4f5a6b"
VERSION = "GitHub Copilot CLI 9.9.9."
TOKENS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
ISOLATION = ["--no-ask-user", "--no-auto-update", "--no-custom-instructions", "--disable-builtin-mcps",
             "--disallow-temp-dir"]
DENIED = [f"write({path})" for path in ("_bmad/**", ".agents/**", ".claude/**", ".bmad-coordinator/**", "AGENTS.md",
                                        "CLAUDE.md", ".github/**")]
DENY_ARGS = [item for rule in DENIED for item in ("--deny-tool", rule)]
RETURN = "/r/worker-return/return.json"
WRITE = [["tool.execution_start", {"toolCallId": "w1", "toolName": "create", "arguments": {"path": "@return"}}],
         ["tool.execution_complete", {"toolCallId": "w1", "success": True}]]  # bundled schema: no toolName here
READ = [["tool.execution_start", {"toolCallId": "r1", "toolName": "view", "arguments": {"path": "brief.md"}}]]
MARKERS = ("AI_AGENT", "GIT_EDITOR", "COREPACK_ENABLE_AUTO_PIN", "NoDefaultCurrentDirectoryInExePath")
PARENT = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "parent-token-value", "CLAUDE_CONFIG_DIR": "/private/cfg",
          "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-value", "ANTHROPIC_API_KEY": "sk-ant-secret-value",
          "AI_AGENT": "claude-code_parent", "GIT_EDITOR": "true", "SCRUB_DECOY": "decoy"}
USAGE = {"premiumRequests": 1, "totalApiDurationMs": 12, "sessionDurationMs": 34}
UV = "uv run --no-project "
# The return-schema listing as the workers see it, pinned literally (return-schema round review, minor 2).
GENERIC_KEYS = ("`affected_ids`: array of string; `assignment_id`: string; `changed_paths`: array of string; `checks`: "
                "array of string; `descendants`: array of string; `options`: array of string; `request_fingerprint`: "
                "string; `result`: one of \"completed\", \"needs-decision\", \"blocked\"; `unresolved_question`: "
                "string or null")
PLANNING_KEYS = ("`affected_ids`: array of string; `assignment_id`: string; `changed_paths`: array of string; "
                 "`checks`: array of string; `customization`: array of object {`sha256`: string, `skill`: string}; "
                 "`descendants`: array of string; `options`: array of string; `request_fingerprint`: string; `result`: "
                 "one of \"completed\", \"needs-decision\", \"blocked\"; `unresolved_question`: string or null; "
                 "`workflow_json`: string")


def listing(keys: str, skeleton: str) -> str:
    return f"The return object has exactly these keys, all required: {keys}. Skeleton (fill every value): `{skeleton}`."


def generic_skeleton(assignment_id: str, fingerprint: str) -> str:
    return (f'{{"affected_ids": [], "assignment_id": "{assignment_id}", "changed_paths": [], "checks": [], '
            f'"descendants": [], "options": [], "request_fingerprint": "{fingerprint}", '
            '"result": "<completed|needs-decision|blocked>", "unresolved_question": null}')


def planning_skeleton(assignment_id: str, fingerprint: str) -> str:
    return (f'{{"affected_ids": [], "assignment_id": "{assignment_id}", "changed_paths": [], "checks": [], '
            '"customization": [{"sha256": "<string>", "skill": "<string>"}], "descendants": [], "options": [], '
            f'"request_fingerprint": "{fingerprint}", "result": "<completed|needs-decision|blocked>", '
            '"unresolved_question": null, "workflow_json": "<string>"}')
# P0c (Copilot CLI 1.0.91, smoke-copilot-1/p0c/run/events.jsonl): the tool set offered to the model, from the
# `session.usage_checkpoint` record (promptCacheBreakState[0].models["claude-sonnet-5"].tools[].name); the stream has
# no init record listing tools.
P0C_TOOLS = ["bash", "read_bash", "stop_bash", "list_bash", "view", "create", "edit", "web_fetch",
             "fetch_copilot_cli_documentation", "search_code_subagent", "skill", "run_dynamic_workflow",
             "dynamic_workflows_manage", "sql", "session_store_sql", "read_agent", "list_agents", "write_agent", "grep",
             "glob", "task"]
# Ruling R2a: the author pin drops every tool that reaches the network or spawns or drives agents or workflows, and
# `session_store_sql` (past sessions: an account-context leak route).
EXCLUDED = ["web_fetch", "fetch_copilot_cli_documentation", "task", "search_code_subagent", "read_agent", "list_agents",
            "write_agent", "run_dynamic_workflow", "dynamic_workflows_manage", "session_store_sql"]
AUTHOR_TOOLS = [name for name in P0C_TOOLS if name not in EXCLUDED]
# Live record shapes copied from the same file (lines 115/117, 182/192, 333/360; `toolTelemetry` and long
# `detailedContent` dropped, paths and ids verbatim): denials appear only as tool.execution_complete with
# error.code "denied" (no permission.* record and no session.start in stdout).
LIVE = "/var/tmp/copilot-p0c.9qu8ir_s"
BMAD_START = {"type": "tool.execution_start", "data": {
    "toolCallId": "toolu_01PoQmjKFhN2Njfj7e9znqEm", "toolName": "create",
    "arguments": {"file_text": "probe", "path": f"{LIVE}/cwd/_bmad/probe.txt"}, "turnId": "3",
    "model": "claude-sonnet-5", "toolTitle": "Creating file"}, "id": "42e03624-0f45-446c-ae92-0514e2c33054",
    "timestamp": "2026-10-05T22:31:40.459Z", "parentId": "0a0ba6eb-6c20-4a4f-9ede-5de17cab68ae"}
BMAD_ERROR = {"message": "Permission to run this tool was denied due to the following rules: `write(_bmad/**)`",
              "code": "denied"}
BMAD_DENIED = {"type": "tool.execution_complete", "data": {
    "toolCallId": "toolu_01PoQmjKFhN2Njfj7e9znqEm", "model": "claude-sonnet-5",
    "interactionId": "bed7f67e-5b29-48ad-a277-7eb2bbf311cf", "turnId": "3", "rte": True, "success": False,
    "error": BMAD_ERROR}, "id": "b862dfac-f94d-4779-ba8f-4c4165f526f3", "timestamp": "2026-10-05T22:31:40.462Z",
    "parentId": "42e03624-0f45-446c-ae92-0514e2c33054"}
UV_START = {"type": "tool.execution_start", "data": {
    "toolCallId": "toolu_01HnGGzxhDbRbK1GcBeheKCu", "toolName": "bash",
    "arguments": {"command": f"uv run --no-project {LIVE}/cwd/probe.py", "description": "Run probe.py with no args"},
    "turnId": "6", "model": "claude-sonnet-5", "toolTitle": "Running command",
    "shellToolInfo": {"possiblePaths": ["run", f"{LIVE}/cwd/probe.py"], "hasWriteFileRedirection": False}},
    "id": "5efb1a8b-3774-4163-beac-14adbd0d368c", "timestamp": "2026-10-05T22:31:48.761Z",
    "parentId": "0483c94b-1e5a-4c7b-b1ba-2983263c5801"}
UV_ERROR = {"message": "Permission denied and could not request permission from user. Copilot needed the user's "
                       "approval and no one could answer. This is not an OS or sandbox error.", "code": "denied"}
UV_DENIED = {"type": "tool.execution_complete", "data": {
    "toolCallId": "toolu_01HnGGzxhDbRbK1GcBeheKCu", "model": "claude-sonnet-5",
    "interactionId": "bed7f67e-5b29-48ad-a277-7eb2bbf311cf", "turnId": "6", "rte": True, "success": False,
    "error": UV_ERROR}, "id": "96045c5c-f78a-442d-9da0-291872671929", "timestamp": "2026-10-05T22:31:48.986Z",
    "parentId": "126a2662-2dd7-4a09-beff-bdd43f2fbaf4"}
GIT_START = {"type": "tool.execution_start", "data": {
    "toolCallId": "toolu_01NnBYJj2oYRZjYagtCLyLqT", "toolName": "bash",
    "arguments": {"command": "git status", "description": "Check git status"}, "turnId": "10",
    "model": "claude-sonnet-5", "toolTitle": "Running command",
    "shellToolInfo": {"possiblePaths": ["status"], "hasWriteFileRedirection": False}},
    "id": "a0e90b3a-2d22-4843-b088-997cecaee517", "timestamp": "2026-10-05T22:31:57.370Z",
    "parentId": "3211ca6f-81b7-465f-8008-c192e81cca59"}
GIT_DONE = {"type": "tool.execution_complete", "data": {
    "toolCallId": "toolu_01NnBYJj2oYRZjYagtCLyLqT", "model": "claude-sonnet-5",
    "interactionId": "bed7f67e-5b29-48ad-a277-7eb2bbf311cf", "turnId": "10", "rte": True,
    "shellExecution": {"exitCode": 0}, "success": True, "result": {"content": "On branch master\n"}},
    "id": "3adaf547-0ac1-4d70-acc1-d7a77f14f3a2", "timestamp": "2026-10-05T22:31:57.401Z",
    "parentId": "a0e90b3a-2d22-4843-b088-997cecaee517"}
# A fake `uv` that records each call to <bin>/../uv.jsonl: argv, cwd, every environment name (LC_* aside: Python
# coerces the C locale itself) and the PATH and HOME values. The launcher passes uv no test variable, so the output
# path is fixed rather than taken from the environment.
UV_RECORDER = """#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "uv.jsonl"), "a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                          "names": sorted(key for key in os.environ if not key.startswith("LC_")),
                          "path": os.environ.get("PATH"), "home": os.environ.get("HOME"),
                          "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE")}) + "\\n")
"""
REAL_UV = shutil.which("uv")  # the operator's uv: a stated prerequisite of the planted uv.toml test (never skipped)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def probed(*names: str) -> clients.Client:
    """The record as a reviewed P0c change would set it: internal_tools from an observed vocabulary."""
    allowed = frozenset(names)
    return replace(clients.COPILOT, internal_tools=allowed,
                   allowed_tools=lambda mode: allowed if mode == "workspace-write" else frozenset())


class CopilotClientTests(Harness):
    CLIENT = COPILOT

    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ)  # restored afterwards; no inherited Copilot or token variable leaks in
        environ.start()
        self.addCleanup(environ.stop)
        for name in [name for name in os.environ if name.startswith(("COPILOT", "CLAUDE", "ANTHROPIC")) or
                     name in TOKENS or name in MARKERS]:
            del os.environ[name]  # the scrubbed families are planted explicitly where a test needs them
        self.home = self.root / "home"
        self.home.mkdir()
        self.env["HOME"] = str(self.home)

    # -- helpers -------------------------------------------------------------------------------------------------

    def author(self, t=None, key="one", **env):
        assignment = self.assign(t, idempotency_key=key)
        return assignment, self.go(assignment, t, **{"FAKE_WRITE": writes(("result.md", key)), **env})

    def held(self, report, assignment):
        self.assertEqual("unknown", report.state, report.evidence_needs)
        self.assertEqual("running", self.state(assignment))

    def failing(self, name, **env):
        t = self.tree(name)
        assignment, report = self.author(t, name, **env)
        self.held(report, assignment)
        return report

    def launch_spec(self, mode="workspace-write", **over):
        return replace(clients.LaunchSpec("/bin/copilot", mode, Path("/w"), Path("/r"), {"type": "object"},
                                          "gpt-fake", None, SESSION, (), (), return_path=RETURN), **over)

    def facts(self, report) -> dict:
        return self.receipt(report)["client_facts"]

    def argv(self, report) -> list:
        return self.receipt(report, "launch.json")["argv"]

    def planned(self, assignment, **env):
        final = {"result": "completed", "workflow_json": json.dumps(flow()), "unresolved_question": None,
                 "customization": self.custom()}
        return self.run_it(assignment, FAKE_WRITE=writes(*self.first_files()), FAKE_EXTRA=json.dumps(final), **env)

    def fresh(self, **over):
        value = {"sessions": [SESSION], "home_session_after": False,
                 "workspace": {"sha256": "0" * 64, "cwd": "/w", "created_at": "2026-10-05T10:00:00.123Z"}}
        for key in ("cwd", "created_at"):
            if key in over:
                value["workspace"][key] = over.pop(key)
        return {**value, **over}

    def records(self, *middle, result=None):
        start = {"type": "session.start", "data": {"sessionId": SESSION, "copilotVersion": "9.9.9"}}
        last = {"type": "result", "sessionId": SESSION, "exitCode": 0, "usage": USAGE, **(result or {})}
        return [start, *middle, last]

    def terminal(self, records, fresh=None, **launch):
        directory = self.root / f"direct-{uuid.uuid4().hex}"
        (directory / clients.WORKER_RETURN).mkdir(parents=True)
        (directory / "events.jsonl").write_bytes(b"".join(
            (item if isinstance(item, bytes) else json.dumps(item).encode()) + b"\n" for item in records))
        (directory / "freshness.json").write_bytes(canonical(self.fresh() if fresh is None else fresh))
        (directory / clients.WORKER_RETURN / "return.json").write_text('{"ok": true}')
        recorded = {"session_id": SESSION, "worktree": "/w", "return_path": RETURN,
                    "pre_spawn_at": "2026-10-05T10:00:00.123456+00:00", "home_session_preexisted": False, **launch}
        return clients.COPILOT.terminal(directory, recorded)

    def skeleton(self, text) -> dict:
        """The JSON skeleton a return-schema listing prints (worker.RETURN_SCHEMA)."""
        return json.loads(text.split("Skeleton (fill every value): `", 1)[1].split("`", 1)[0])

    def unknown(self, records, fresh=None, **launch):
        self.code("UNKNOWN_EVIDENCE", self.terminal, records, fresh, **launch)

    # -- argv, env and modes -----------------------------------------------------------------------------------------

    def test_author_argv_flags_env_unset_run_env_and_exact_shell_rules_without_forbidden_flags(self):
        commands = ("uv run --no-project /w/_bmad/scripts/resolve_customization.py --skill /w/.agents/skills/bmad-prd",
                    "uv run --no-project /w/_bmad/scripts/resolve_config.py --project-root /w",
                    "uv run --no-project /w/_bmad/scripts/memlog.py init --workspace ws",
                    "uv run --no-project /w/_bmad/scripts/memlog.py append --workspace ws")
        head = ["/bin/copilot", "-p", clients.ENVELOPE_ARG, "--output-format", "json", "--session-id", SESSION,
                "--model", "gpt-fake", *ISOLATION, "--log-dir", "/r/copilot-logs", "-C", "/w",
                "--add-dir", "/r/worker-return", "--available-tools", ",".join(sorted(AUTHOR_TOOLS)),
                "--allow-tool", "write"]
        rules = ["--allow-tool", "shell(bmad-script)"]  # ruling R3: the launcher's bare name, never `uv`
        self.assertEqual(head + rules + ["--allow-tool", "shell(sha256sum)"] + DENY_ARGS,
                         clients.COPILOT.argv(self.launch_spec(commands=commands)))
        self.assertEqual(head + DENY_ARGS, clients.COPILOT.argv(self.launch_spec()))  # generic: no shell rule
        self.assertEqual(DENIED, clients.COPILOT_DENIED)
        for spec in (self.launch_spec("read-only"), self.launch_spec("read-only", isolated=True),
                     self.launch_spec(isolated=True)):
            with self.subTest(mode=spec.mode, isolated=spec.isolated):
                self.code("UNSUPPORTED_CLIENT_MODE", clients.COPILOT.argv, spec)  # no reviewer or isolated argv
        record = clients.COPILOT
        self.assertEqual(("copilot", "copilot", "approval-policy-advisory", True, "worker-file", "argv", 100_000,
                          False, None),
                         (record.name, record.executable, record.confinement, record.presets_session,
                          record.return_source, record.envelope_via, record.max_envelope_bytes,
                          record.write_free_readonly, record.max_turns_default))
        self.assertEqual(({"COPILOT_AUTO_UPDATE": "false"}, frozenset({"COPILOT*", "COPILOT_ALLOW_ALL"})),
                         (record.env, record.env_unset))
        self.assertEqual({"COPILOT_HOME": "/r/copilot-home"}, record.run_env(self.launch_spec()))
        self.assertEqual((("copilot-home", "copilot-logs", "worker-return"), ("copilot-home", "copilot-logs"),
                          "freshness.json", frozenset(AUTHOR_TOOLS), True),
                         (record.run_subdirs, record.private_subdirs, record.observation_name, record.internal_tools,
                          record.script_launcher))
        self.assertEqual((frozenset(AUTHOR_TOOLS), frozenset()), (record.allowed_tools("workspace-write"),
                                                               record.allowed_tools("read-only")))
        self.assertFalse(set(head + rules) & set(FORBIDDEN[COPILOT]))
        self.planning()
        prepared = self.prep("p1", client=COPILOT)
        binding = self.binding_of(prepared["assignment_id"])
        w = binding["worktree"]
        expected = [f"uv run --no-project {w}/_bmad/scripts/resolve_customization.py --skill {w}/.agents/skills/bmad-prd",
                    f"uv run --no-project {w}/_bmad/scripts/resolve_config.py --project-root {w}",
                    f"uv run --no-project {w}/_bmad/scripts/memlog.py init --workspace {WS}",
                    f"uv run --no-project {w}/_bmad/scripts/memlog.py append --workspace {WS}"]
        self.assertEqual((COPILOT, expected), (binding["client"], binding["commands"]))
        out = self.root / "env.json"
        report = self.planned(prepared["assignment_id"], COPILOT_ALLOW_ALL="true", FAKE_ENV_OUT=str(out))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)  # the fake checks the argv grammar
        argv, directory = self.argv(report), Path(report.run_dir)
        self.assertEqual(["write", "shell(bmad-script)", "shell(sha256sum)"],
                         [argv[index + 1] for index, item in enumerate(argv) if item == "--allow-tool"])
        self.assertEqual(str(self.fake_dir / "copilot"), argv[0])
        self.assertFalse(set(argv) & set(FORBIDDEN[COPILOT]))
        env = json.loads(out.read_text())["env"]
        self.assertEqual((str(directory / "copilot-home"), "false", None),
                         (env["COPILOT_HOME"], env["COPILOT_AUTO_UPDATE"], env["COPILOT_ALLOW_ALL"]))
        launch = self.receipt(report, "launch.json")
        self.assertEqual((["COPILOT_AUTO_UPDATE", "COPILOT_HOME", "PATH"], ["COPILOT*", "COPILOT_ALLOW_ALL"]),
                         (launch["env_names"], launch["env_unset"]))  # PATH: a bound author's launcher dir first
        self.assertIn("COPILOT_ALLOW_ALL", launch["isolation"]["removed_names"])
        self.assertEqual(("workspace-write", "approval-policy-advisory", ISOLATION, DENIED),
                         (launch["requested_mode"], launch["confinement"], launch["isolation"]["flags"],
                          launch["isolation"]["deny_rules"]))
        self.assertEqual(DENIED, [argv[index + 1] for index, item in enumerate(argv) if item == "--deny-tool"])
        other = self.assign(idempotency_key="turns")
        self.code("INVALID_INPUT", self.go, other, max_turns=3)  # no max-turns flag: refused before reservation
        self.assertEqual(("prepared", []), (self.state(other), self.reserved(other)))

    def test_modified_allowed_script_is_unknown_through_contract_digests(self):
        self.planning()
        assignment = self.prep("p1", client=COPILOT)["assignment_id"]
        w = self.binding_of(assignment)["worktree"]
        named = sorted({item.split(f"{w}/", 1)[1].split()[0] for item in self.binding_of(assignment)["commands"]})
        self.assertEqual(["_bmad/scripts/memlog.py", "_bmad/scripts/resolve_config.py",
                          "_bmad/scripts/resolve_customization.py"], named)
        self.assertLessEqual(set(named), {item["path"] for item in self.contracts(assignment)})
        report = self.planned(assignment, FAKE_OUTSIDE_WRITE=json.dumps([f"{w}/_bmad/scripts/memlog.py"]))
        self.held(report, assignment)  # the deny rules are advisory; the contract digests fail closed
        self.assertIn("Immutable source or contract bytes changed", report.evidence_needs[0])
        self.assertEqual(DENIED, [item for index, item in enumerate(self.argv(report))
                                  if index and self.argv(report)[index - 1] == "--deny-tool"])

    def test_copilot_planning_brief_names_return_path_exact_shell_forms_and_drops_bytecode_rule(self):
        self.planning()
        codex = self.prep("p1")
        literals = {str(self.root.resolve()): "<ROOT>", str(self.root): "<ROOT>"}
        self.assertEqual((GOLDEN / "planning" / "brief.md").read_bytes(),
                         golden_normalize((self.t.path / codex["brief_ref"]).read_bytes(), literals))
        prepared = self.prep("p2", slug="copilot", client=COPILOT)
        text = self.brief_of(prepared)
        self.assertIn(planning.COPILOT_LABEL, text)
        self.assertNotIn("does not auto-discover", text)  # P0c: worktree `.agents/skills` load as project skills
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", text)
        commands = self.binding_of(prepared["assignment_id"])["commands"]
        self.assertEqual(4, len(commands))
        for command in commands:
            self.assertTrue(command.startswith(UV))
            self.assertIn(f"`bmad-script {command[len(UV):]} …`", text)  # bare name first (ruling R3a)
            self.assertNotIn(f"`{command} …`", text)
        self.assertIn(", and `sha256sum` on stdin only (pipe the customization output into it). Call it by that bare "
                      "name only: a call by its full path is denied. A direct `uv` call is denied, and so is any other "
                      "command that needs approval (Copilot runs some read-only commands, such as `git status`, "
                      "without a rule); use the skill's documented fallback.\n", text)  # M2; return-schema ruling 2
        self.assertIn(planning.LAUNCHER_RULE + "\n", text)
        self.assertNotIn(planning.PLACEHOLDER_RULE, text)
        self.assertIn("run `bmad-script ", text)  # the customization sha256 command also goes through it
        self.assertNotIn("script_launcher", text)  # return-schema ruling 2: no absolute-path fallback (always denied)
        schema = worker._schema(planning.PLANNING_PROFILE)  # ruling 1: the full schema, from the object _final enforces
        self.assertIn("- " + listing(PLANNING_KEYS, planning_skeleton("<uuid4>", "<sha256 hex>")) + "\n", text)
        self.assertEqual(sorted(schema["required"]), sorted(self.skeleton(text)))
        self.assertNotIn("`uv run --no-project /", text)  # no runnable uv form is printed
        self.assertIn("- Write your return JSON, one object, to the envelope's `return_path` and nowhere else, with "
                      "your file-creation tool (`create`, or `edit` to fix it); a shell redirect is accepted but "
                      "discouraged. Your final message is never read as the return.\n", text)  # return-write ruling 2
        self.assertNotIn("`uv` calls other than these are denied", text)  # the Claude sentence
        self.assertNotIn("sandbox", text)
        for rule in planning.RETURN_RULES:
            self.assertIn(rule + "\n", text)
        self.assertEqual(1, text.count(planning.AFFECTED_IDS_RULE + "\n"))

    def test_copilot_brief_and_envelope_print_the_workflow_json_skeleton_for_prd_and_architecture(self):
        """workflow_json skeleton round (attempt 4, S3'': the inner object dropped `prd` and `memlog`): a worker-file
        brief and envelope print the key set `check` enforces, with the artifact key and the exact absolute paths."""
        self.planning()
        w = str(self.t.path)
        expected = {}
        for name, over, key, kind, artifact in (
                ("prd", {}, "prd", "prd", "prd.md"),
                ("arch", {"skill": ARCH, "altitude": "feature", "purpose": "build-substrate"}, "spine",
                 "architecture", "ARCHITECTURE-SPINE.md")):
            prepared = self.prep("w-" + name, slug="w" + name, client=COPILOT, **over)
            ws = f"{SCOPE}/w{name}/{kind}"
            skeleton = (f'`{{"intent": "create", "status": "<complete|partial|blocked>", "open_questions": [], '
                        f'"conflicts_with_prior_decisions": [], "assumptions": [], "external_handoffs": [], '
                        f'"{key}": "{w}/{ws}/{artifact}", "memlog": "{w}/{ws}/.memlog.md"}}`')
            text = self.brief_of(prepared)
            with self.subTest(brief=name):
                self.assertIn(f"- `workflow_json` is one JSON object serialized into one string, with these keys: "
                              f"`intent`, `status`, `open_questions`, `conflicts_with_prior_decisions`, `assumptions`, "
                              f"`external_handoffs`, `{key}`, `memlog`. Skeleton (fill the lists, pick the status): "
                              f"{skeleton}. `{key}` is the artifact (`{artifact}`) and `memlog` the memlog "
                              "(`.memlog.md`), each as the exact absolute path shown, under the worktree and with no "
                              "`{...}` placeholder; both are required unless `status` is `blocked`.\n", text)
                self.assertIn("- Do not call python3, node or uv directly and do not redirect to /tmp; they are denied "
                              "and waste turns. Use the bare launcher and your file tools.\n", text)
            expected[name] = (prepared["assignment_id"], skeleton)
        claude_prepared = self.prep("w-claude", slug="wclaude", client="claude-code")
        claude = self.brief_of(claude_prepared)
        codex = self.brief_of(self.prep("w-codex", slug="wcodex"))
        for text in (claude, codex):  # Codex and Claude briefs carry neither the skeleton nor the tooling line
            self.assertNotIn("serialized into one string, with these keys", text)
            self.assertNotIn("Do not call python3, node or uv directly", text)
        runs = {}
        for name, assignment, slug in (("prd", expected["prd"][0], "wprd"),
                                       ("claude", claude_prepared["assignment_id"], "wclaude")):
            final = {"result": "completed", "workflow_json": json.dumps(flow(slug=slug)), "unresolved_question": None,
                     "customization": self.custom()}
            runs[name] = self.run_it(assignment, FAKE_WRITE=writes(*self.first_files(slug)),
                                     FAKE_EXTRA=json.dumps(final))
            self.assertEqual("completed-claim", runs[name].state, runs[name].evidence_needs)
        report = runs["prd"]
        claude_envelope = json.loads((Path(runs["claude"].run_dir) / "envelope.json").read_bytes())
        self.assertEqual(worker.INSTRUCTIONS, claude_envelope["instructions"])  # skeleton minors m3: no hint
        arch = self.run_it(expected["arch"][0])  # the envelope is written before any classification
        self.assertEqual("unknown", arch.state)  # by design: only the envelope bytes are asserted
        for name, run in (("prd", report), ("arch", arch)):
            with self.subTest(envelope=name):
                envelope = json.loads((Path(run.run_dir) / "envelope.json").read_bytes())
                self.assertIn(f"Skeleton (fill the lists, pick the status): {expected[name][1]}.",
                              envelope["instructions"])

    def test_printed_workflow_skeleton_round_trips_through_the_planning_check(self):
        """Skeleton minors m2/m4: the printed skeleton, with only the status chosen, passes `check` for PRD and
        architecture; the status placeholder pasted as it is fails closed; RETURN_RULES[0] (fixed bytes for Codex and
        Claude) still names every list and status the constants hold."""
        for skill, key in (("bmad-prd", "prd"), (ARCH, "spine")):
            files = planning.workflow_files("ws/x", skill)
            binding = {"skill": skill, "intent": "create", "worktree": "/w", **files, "memlog_append": True,
                       "expected_customization": [{"skill": skill, "sha256": "a" * 64}]}
            printed = planning.workflow_skeleton_text(binding)
            workflow = json.loads(printed.split("pick the status): `", 1)[1].split("`", 1)[0])
            self.assertEqual((f"/w/{files['artifact']}", "/w/ws/x/.memlog.md"), (workflow[key], workflow["memlog"]))
            final = {"result": "completed", "unresolved_question": None, "options": [],
                     "customization": binding["expected_customization"]}
            changed = [files["artifact"], files["memlog"]]
            with self.subTest(skill=skill):
                payload = planning.check({**final, "workflow_json": json.dumps({**workflow, "status": "complete"})},
                                         {"source_digests_json": "[]"}, binding, None, changed)
                self.assertEqual(("OBSERVED_COMPLETE_CLAIM", changed, "match"),
                                 (payload["verdict"], payload["artifacts"], payload["customization_check"]))
                pasted = {**final, "workflow_json": json.dumps(workflow)}  # the status placeholder left in
                refused = self.code("UNKNOWN_EVIDENCE", planning.check, pasted, {"source_digests_json": "[]"}, binding,
                                    None, changed)
                self.assertIn("unknown BMAD status", refused.message)
        for item in planning.WORKFLOW_LISTS + planning.WORKFLOW_STATUSES:
            self.assertIn(f"`{item}`", planning.RETURN_RULES[0])

    def test_colon_in_rule_paths_is_refused_before_any_write(self):
        self.planning()
        before = len(self.store.read_snapshot()["assignments"])
        for client in (COPILOT, "claude-code"):
            with self.subTest(client=client):
                self.code("INVALID_INPUT", self.prep, "p-colon", slug="colon", client=client, client_root="a:b")
        self.assertEqual(before, len(self.store.read_snapshot()["assignments"]))
        self.assertFalse(clients.pattern_safe("/w:x"))
        self.code("INVALID_INPUT", planning.author_commands, "/w:x", ".agents/skills", "bmad-prd", "ws/prd")
        self.code("INVALID_INPUT", clients.bound_commands, {"commands": ["uv run x:y"]})

    def test_generic_worker_file_envelope_names_the_return_path_and_codex_instructions_are_unchanged(self):
        _, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory = Path(report.run_dir)
        envelope = json.loads((directory / "envelope.json").read_bytes())
        path = str(directory / clients.WORKER_RETURN / "return.json")
        schema = json.loads((directory / "return.schema.json").read_bytes())  # the object _final enforces
        ids = (envelope["assignment_id"], envelope["request_fingerprint"])
        self.assertEqual((path, worker.INSTRUCTIONS + " " + worker.RETURN_FILE.format(path) + " " +
                          listing(GENERIC_KEYS, generic_skeleton(*ids))),
                         (envelope["return_path"], envelope["instructions"]))
        self.assertIn(f"`{path}`", envelope["instructions"])
        self.assertIn(f"Write the required JSON object, and nothing else, to `{path}` (this envelope's `return_path`) "
                      "and nowhere else, with your file-creation tool (for example `create`, or `edit` to fix it); a "
                      "shell redirect is accepted but discouraged. Your final message is never read as the return.",
                      envelope["instructions"])  # pinned literally (return-write fix 1, F5a)
        self.assertEqual(sorted(schema["required"]), sorted(self.skeleton(envelope["instructions"])))  # ruling 1
        self.assertEqual(sorted(worker.FIELDS), sorted(schema["required"]))
        for key in schema["required"]:
            self.assertIn(f"`{key}`: ", envelope["instructions"])
        self.assertEqual("client-file", clients.CODEX.return_source)  # Codex is no worker-file client
        for client in (clients.DEFAULT, "claude-code"):  # Codex and Claude envelopes are unchanged
            with self.subTest(client=client):
                t = self.tree("plain-" + client)
                _, other = self.author(t, client, client=client)
                self.assertEqual("completed-claim", other.state, other.evidence_needs)
                envelope = json.loads((Path(other.run_dir) / "envelope.json").read_bytes())
                self.assertEqual(worker.INSTRUCTIONS, envelope["instructions"])
                self.assertFalse({"return_path", "script_launcher"} & set(envelope))

    def test_parent_session_markers_never_reach_copilot_or_codex_and_codex_launch_keys_are_unchanged(self):
        out = self.root / "env.json"
        removed = {"CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                   "ANTHROPIC_API_KEY", "AI_AGENT", "GIT_EDITOR"}
        for client in (COPILOT, clients.DEFAULT):
            with self.subTest(client=client):
                t = self.tree("parent-" + client)
                _, report = self.author(t, client, client=client, FAKE_ENV_OUT=str(out), **PARENT)
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                seen = set(json.loads(out.read_text())["names"])
                self.assertFalse(removed & seen)
                self.assertIn("SCRUB_DECOY", seen)  # unrelated variables pass
                scrub = self.receipt(report, "env-scrub.json")
                self.assertEqual(sorted(removed), scrub["removed_names"])
                self.assertEqual(0o600, (Path(report.run_dir) / "env-scrub.json").stat().st_mode & 0o777)
                for path in Path(report.run_dir).rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        for value in ("parent-token-value", "oauth-secret-value", "sk-ant-secret-value"):
                            self.assertNotIn(value.encode(), path.read_bytes(), path.name)
                launch = self.receipt(report, "launch.json")
                if client == COPILOT:
                    self.assertLessEqual(removed, set(launch["isolation"]["removed_names"]))
                else:  # a Codex launch.json keeps exactly the golden keys plus client and binary
                    keys = json.loads((GOLDEN / "generic" / "launch-keys.json").read_text())
                    self.assertEqual(set(keys) | {"client", "binary"}, set(launch))
        self.assertEqual((frozenset(), clients.PARENT_MARKERS), (clients.CODEX.env_unset, clients.CODEX.session_scrub))
        self.assertEqual(frozenset(), clients.CLAUDE.session_scrub)  # Claude keeps its own scrub and ANTHROPIC*

    def test_only_worker_return_dir_is_added_and_holds_no_github_dir(self):
        out = self.root / "env.json"
        assignment, report = self.author(FAKE_ENV_OUT=str(out))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        argv, directory = self.argv(report), Path(report.run_dir)
        self.assertEqual([str(directory / clients.WORKER_RETURN)],
                         [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"])
        info = json.loads(out.read_text())
        self.assertEqual(({"entries": [], "mode": 0o700}, {"entries": [], "mode": 0o700}),
                         (info["worker_return"], info["copilot_home"]))  # both empty at spawn
        self.assertEqual(["return.json"], os.listdir(directory / clients.WORKER_RETURN))
        envelope = json.loads((directory / "envelope.json").read_bytes())
        self.assertEqual(str(directory / clients.WORKER_RETURN / "return.json"), envelope["return_path"])
        self.assertEqual(envelope["return_path"], self.receipt(report, "launch.json")["return_path"])
        report = self.failing("github", FAKE_RETURN="github-dir")  # a trusted-looking .github beside the return
        self.assertTrue((Path(report.run_dir) / clients.WORKER_RETURN / ".github" / "agents").is_dir())

    def test_review_prepare_reserve_and_isolated_run_refuse_copilot_with_unsupported_client_mode(self):
        self.planning()
        author = self.prep("p1")["assignment_id"]
        report = self.execute(author, flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.bundled()["id"]
        before, versions = len(self.store.read_snapshot()["assignments"]), self.version_count()
        self.code("UNSUPPORTED_CLIENT_MODE", self.prepared, bundle, "r1", client=COPILOT)
        self.assertEqual(before, len(self.store.read_snapshot()["assignments"]))
        reader = self.assign(idempotency_key="reader", permission_mode="read-only", writable_paths=[])
        self.code("UNSUPPORTED_CLIENT_MODE", self.coordinator.reserve_launch, self.owner, reader, COPILOT)
        self.code("UNSUPPORTED_CLIENT_MODE", self.go, reader)
        self.cli("assignment", "reserve-launch", reader, "--token-file", str(self.token), "--client", COPILOT, code=4)
        self.assertEqual(("prepared", []), (self.state(reader), self.reserved(reader)))
        self.assertFalse(os.path.lexists(self.run_dir(reader)))
        root = self.root / "isolated"
        cwd = root / "cwd"
        cwd.mkdir(parents=True)
        subprocess.run(["git", "-C", str(cwd), "init", "-q"], check=True)
        with patch.dict(os.environ, self.env):
            self.code("UNSUPPORTED_CLIENT_MODE", clients.run_isolated, COPILOT, self.t.repo, root, cwd,
                      root / "receipts", {"role": "judge"}, worker._schema(), "fake-model", 5)
        self.assertFalse((root / "receipts").exists())
        self.assertEqual(versions, self.version_count())  # refused before any preflight

    def test_copilot_author_reviewed_by_claude_fake_is_observed_and_independent(self):
        self.planning()
        author = self.prep("p1", client=COPILOT)["assignment_id"]
        report = self.planned(author)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.bundled()["id"]
        reviewer = self.prepared(bundle, "r1", client="claude-code")["assignment_id"]
        report = self.run_it(reviewer, FAKE_EXTRA=json.dumps(self.final_for(reviewer)))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        recorded = self.record(reviewer)
        self.assertEqual("observed", recorded["provenance_class"])
        sources = [self.detail(item)["return"]["source_id"] for item in (author, reviewer)]
        self.assertEqual([COPILOT, "claude-code"], [clients.client_of(item) for item in sources])
        self.assertNotEqual(*sources)
        self.assertEqual(([COPILOT], ["claude-code"]), (self.reserved(author), self.reserved(reviewer)))
        review.accept_bound(self.store, self.owner, bundle, recorded["id"])
        self.assertEqual("current", self.coordinator.verify().current)

    def test_recorded_argv_replaces_envelope_with_reference_and_envelope_digest_binds_it(self):
        received = self.root / "envelope.sha256"
        assignment, report = self.author(FAKE_ENVELOPE_OUT=str(received))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory = Path(report.run_dir)
        envelope = (directory / "envelope.json").read_bytes()
        launch_bytes = (directory / "launch.json").read_bytes()
        launch = json.loads(launch_bytes)
        self.assertEqual(clients.ENVELOPE_ARG, launch["argv"][launch["argv"].index("-p") + 1])
        self.assertEqual((sha(envelope), sha(envelope)), (received.read_text(), launch["envelope_sha256"]))
        self.assertNotIn(b"Do the assigned work.", launch_bytes)  # the brief text travels only in the argv
        (directory / "envelope.json").write_bytes(envelope.replace(b"}", b" }", 1))
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        (directory / "envelope.json").write_bytes(envelope)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_oversized_envelope_refused_before_preflight_and_reservation(self):
        _, report = self.author()
        size = len((Path(report.run_dir) / "envelope.json").read_bytes())
        self.assertLess(size, 5000)
        (self.t.path / "big.md").write_text("x" * 100_000)
        big = self.assign(idempotency_key="big", brief="big.md")
        versions = self.version_count()
        self.code("INVALID_INPUT", self.go, big)
        self.assertEqual(("prepared", []), (self.state(big), self.reserved(big)))
        self.assertFalse(os.path.lexists(self.run_dir(big)))
        self.assertEqual(versions, self.version_count())
        (self.t.path / "fits.md").write_text("x" * (100_000 - size - 200))  # just under the limit still runs
        fits = self.assign(idempotency_key="fits", brief="fits.md")
        report = self.go(fits, FAKE_WRITE=writes(("result.md", "fits")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertGreater(len((Path(report.run_dir) / "envelope.json").read_bytes()), 99_000)

    # -- return file -------------------------------------------------------------------------------------------------

    def test_return_file_is_the_only_return_and_decoy_final_message_is_ignored(self):
        assignment, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory = Path(report.run_dir)
        decoys = [item for item in clients.jsonl_records(directory / "events.jsonl")
                  if item["type"] == "assistant.message"]
        self.assertEqual(1, len(decoys))
        decoy = json.loads(decoys[0]["data"]["content"])
        self.assertEqual("blocked", decoy["result"])  # a valid-looking return the coordinator never reads
        last = (directory / "last.json").read_bytes()
        self.assertEqual(clients.read_worker_return(directory), last)
        self.assertEqual("completed", json.loads(last)["result"])
        self.assertEqual(clients.worker_return_sha256(directory), self.receipt(report)["worker_return_sha256"])
        self.assertEqual("completed", self.detail(assignment)["return"]["result"])
        missing = self.failing("missing", FAKE_RETURN="missing")  # no return file: the decoy is no fallback
        self.assertFalse((Path(missing.run_dir) / "last.json").exists())

    def test_missing_symlinked_hardlinked_oversized_invalid_schema_invalid_or_extra_return_file_is_unknown(self):
        for kind in ("missing", "symlink", "hardlink", "oversize", "invalid", "generic-invalid", "extra-file"):
            with self.subTest(kind=kind):
                self.failing("return-" + kind, FAKE_RETURN=kind)
        self.failing("bad-schema", FAKE_MODE="bad-schema")

    def test_deep_nesting_return_file_is_unknown(self):
        self.failing("deep", FAKE_RETURN="deep-nesting")
        directory = self.root / "nesting"
        (directory / clients.WORKER_RETURN).mkdir(parents=True)
        target = directory / clients.WORKER_RETURN / "return.json"
        for text, ok in (('{"a": ' + "[" * 63 + "]" * 63 + "}", True), ('{"a": ' + "[" * 64 + "]" * 64 + "}", False),
                         ('{"a": "' + "[" * 100 + '"}', True), ('{"a": ' + "[" * 5000 + "]" * 5000 + "}", False)):
            with self.subTest(length=len(text), ok=ok):
                target.write_text(text)
                if ok:
                    self.assertEqual(canonical(json.loads(text)), clients.read_worker_return(directory))
                else:
                    self.code("UNKNOWN_EVIDENCE", clients.read_worker_return, directory)

    # -- freshness ---------------------------------------------------------------------------------------------------

    def test_fresh_home_session_id_echo_and_workspace_yaml_prove_freshness(self):
        sessions = []
        for key in ("one", "two"):
            _, report = self.author(key=key)
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            directory, launch = Path(report.run_dir), self.receipt(report, "launch.json")
            session = launch["session_id"]
            self.assertEqual((4, session), (uuid.UUID(session).version, str(uuid.UUID(session))))
            self.assertEqual(session, launch["argv"][launch["argv"].index("--session-id") + 1])
            self.assertFalse(launch["home_session_preexisted"])
            workspace = directory / "copilot-home" / "session-state" / session / "workspace.yaml"
            created = workspace.read_text().split("created_at: ", 1)[1].strip()
            self.assertEqual({"sessions": [session], "home_session_after": False,
                              "workspace": {"sha256": sha(workspace.read_bytes()), "cwd": launch["worktree"],
                                            "created_at": created}},
                             json.loads((directory / "freshness.json").read_bytes()))
            start = datetime.fromisoformat(launch["pre_spawn_at"])
            self.assertGreaterEqual(datetime.fromisoformat(created), start.replace(microsecond=start.microsecond // 1000 * 1000))
            terminal = self.receipt(report)
            self.assertEqual(sha((directory / "freshness.json").read_bytes()), terminal["observation_sha256"])
            self.assertEqual((session, f"{COPILOT}:{session}"), (terminal["thread_id"], terminal["source_id"]))
            sessions.append(session)
        self.assertNotEqual(*sessions)
        self.failing("echo", FAKE_MODE="wrong-session")

    def test_preexisting_second_or_leaked_session_cwd_mismatch_or_early_created_at_is_unknown(self):
        for mode in ("preexisting-session", "home-leak", "cwd-mismatch", "early-created"):
            with self.subTest(mode=mode):
                self.failing(mode, FAKE_MODE=mode)
        fixed = uuid.uuid4()
        (self.home / ".copilot" / "session-state" / str(fixed)).mkdir(parents=True)
        t = self.tree("home-before")
        with patch.object(worker, "uuid", SimpleNamespace(uuid4=lambda: fixed)):
            assignment, report = self.author(t, "home-before")
        self.held(report, assignment)
        self.assertTrue(self.receipt(report, "launch.json")["home_session_preexisted"])
        records = self.records()
        self.assertEqual(SESSION, self.terminal(records).thread_id)
        self.assertEqual(SESSION, self.terminal(records, self.fresh(created_at="2026-10-05T10:00:00.123+00:00"))
                         .thread_id)  # equal to the pre-spawn instant truncated to milliseconds
        for name, fresh in (("second", self.fresh(sessions=[SESSION, str(uuid.uuid4())])),
                            ("other", self.fresh(sessions=[str(uuid.uuid4())])), ("none", self.fresh(sessions=[])),
                            ("unlisted", self.fresh(sessions=None)), ("no-workspace", self.fresh(workspace=None)),
                            ("leak", self.fresh(home_session_after=True)),
                            ("leak-unknown", self.fresh(home_session_after=None)),
                            ("cwd", self.fresh(cwd="/elsewhere")), ("no-cwd", self.fresh(cwd=None)),
                            ("early", self.fresh(created_at="2026-10-05T10:00:00.122Z")),
                            ("naive", self.fresh(created_at="2026-10-05T10:00:00.123")),
                            ("garbage", self.fresh(created_at="yesterday")), ("no-created", self.fresh(created_at=None))):
            with self.subTest(name=name):
                self.unknown(records, fresh)
        for launch in ({"home_session_preexisted": True}, {"home_session_preexisted": None}, {"pre_spawn_at": None},
                       {"worktree": "/other"}):
            with self.subTest(launch=launch):
                self.unknown(records, **launch)

    def test_freshness_sealed_once_and_replay_ignores_later_home_changes(self):
        calls = []

        def spy(run_dir, launch):
            calls.append(run_dir)
            return clients.COPILOT.observe(run_dir, launch)

        with patch.dict(clients.REGISTRY, {COPILOT: replace(clients.COPILOT, observe=spy)}):
            assignment, report = self.author()
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            directory = Path(report.run_dir)
            self.assertEqual([directory], calls)
            session = self.receipt(report, "launch.json")["session_id"]
            fresh = directory / "freshness.json"
            self.assertEqual(0o600, fresh.stat().st_mode & 0o777)
            shutil.rmtree(directory / "copilot-home" / "session-state")
            (self.home / ".copilot" / "session-state" / session).mkdir(parents=True)  # a later $HOME leak
            with patch.dict(os.environ, {"HOME": str(self.home)}):  # replay sees the leak yet reads only the seal
                self.assertTrue(clients.COPILOT.observe(directory, self.receipt(report, "launch.json"))
                                ["home_session_after"])
                self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
                self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
                historical_return(self.t.repo, self.store, assignment)
            self.assertEqual(1, len(calls))  # observed once at return; replay reads only the seal
            original = fresh.read_bytes()
            fresh.write_bytes(original.replace(b'"home_session_after":false', b'"home_session_after":true '))
            self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
            fresh.write_bytes(original)
            self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_ended_copilot_run_private_subdirs_outside_d16_baseline_and_worker_return_inside(self):
        assignment, report = self.author(key="first")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory = Path(report.run_dir)
        state = next((directory / "copilot-home").rglob("workspace.yaml"))
        log = directory / "copilot-logs" / "process.log"
        returned = directory / clients.WORKER_RETURN / "return.json"
        self.assertTrue(state.is_file() and log.is_file())
        baseline = clients.sealed_evidence(self.t.repo, "another-run")
        relative = [path.relative_to(self.base_dir).as_posix() for path in (returned, directory / "freshness.json")]
        self.assertLessEqual(set(relative), set(baseline))
        self.assertEqual([], [key for key in baseline if "/copilot-home/" in key or "/copilot-logs/" in key])
        self.assertEqual(("copilot-home", "copilot-logs"), clients.private_subdirs_of(directory))
        t = self.tree("private")
        later = self.assign(t, idempotency_key="private")
        result = self.go(later, t, FAKE_TAMPER=json.dumps([["modify", str(state)], ["modify", str(log)]]),
                         FAKE_WRITE=writes(("result.md", "p")))
        self.assertEqual("completed-claim", result.state, result.evidence_needs)  # private state is no sealed evidence
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        t = self.tree("returned")
        tampered = self.assign(t, idempotency_key="returned")
        result = self.go(tampered, t, FAKE_TAMPER=json.dumps([["modify", str(returned)]]),
                         FAKE_WRITE=writes(("result.md", "r")))
        self.held(result, tampered)
        self.assertEqual([relative[0]], self.receipt(result, "integrity.json")["modified"])

    # -- terminal rule, tools and facts ------------------------------------------------------------------------------

    def test_result_record_last_exit_code_and_session_error_rules(self):
        for mode in ("truncated", "no-result", "session-error", "wrong-session"):
            with self.subTest(mode=mode):
                self.failing(mode, FAKE_MODE=mode)
        report = self.failing("exit-code", FAKE_MODE="exit-code-1")
        self.assertEqual(1, self.receipt(report, "end.json")["exit_code"])
        report = self.failing("exit-code-only", FAKE_MODE="exit-code-only")  # the result record alone decides
        self.assertEqual(0, self.receipt(report, "end.json")["exit_code"])
        directory = Path(report.run_dir)  # a valid return file was written; the terminal refused before reading it
        self.assertEqual("completed", json.loads(clients.read_worker_return(directory))["result"])
        launch = self.receipt(report, "launch.json")
        refused = self.code("UNKNOWN_EVIDENCE", clients.COPILOT.terminal, directory, launch)
        self.assertEqual("the result record's exitCode is not 0", str(refused.__cause__))  # fix 1: distinct cause
        self.assertFalse((directory / "last.json").exists())  # so it was never copied into last.json
        start, result = self.records()
        message = {"type": "assistant.message", "data": {"content": "{}"}}
        self.assertEqual(SESSION, self.terminal([result]).thread_id)  # session.start is not required
        for name, case in (("only-start", [start]), ("result-first", [result, start]), ("two", [start, result, result]),
                           ("after", [start, result, message]), ("non-json", [start, b"not json", result]),
                           ("untyped", [start, {"data": {}}, result]), ("array", [start, b"[]", result]),
                           ("blank", [start, b"", result]),
                           ("error", [start, {"type": "session.error", "data": {}}, result]),
                           ("foreign", [{"type": "session.start", "data": {"sessionId": str(uuid.uuid4())}}, result]),
                           ("foreign-top", [start, {"type": "user.message", "sessionId": "x"}, result])):
            with self.subTest(name=name):
                self.unknown(case)
        for over in ({"exitCode": 1}, {"exitCode": "0"}, {"exitCode": True}, {"exitCode": None}, {"sessionId": None},
                     {"sessionId": str(uuid.uuid4())}):
            with self.subTest(result=over):
                self.unknown([start, {**result, **over}])

    def test_tool_events_outside_allowlist_or_without_return_write_are_unknown_and_absent_events_record_blocked(self):
        _, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertNotIn("tools", self.facts(report))
        self.assertEqual("blocked", self.facts(report)["tool_check"])  # no tool events: never passed
        t = self.tree("p0c-allowlist")  # ruling R2: internal_tools is the P0c vocabulary, so a create passes
        _, report = self.author(t, "p0c-allowlist", FAKE_TOOL_EVENTS=json.dumps(WRITE))
        self.assertEqual((["create"], "passed"), (self.facts(report)["tools"], self.facts(report)["tool_check"]))
        self.failing("shared-knob", FAKE_TOOLS=json.dumps([["Bash", "rm -rf"]]))
        self.assertEqual(frozenset({"create", "edit", "str_replace_editor", "apply_patch"}), clients.COPILOT_WRITE_TOOLS)
        with patch.dict(clients.REGISTRY, {COPILOT: probed("view", "create")}):
            t = self.tree("passed")
            assignment, report = self.author(t, "passed", FAKE_TOOL_EVENTS=json.dumps(READ + WRITE))
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            self.assertEqual((["create", "view"], "passed"),
                             (self.facts(report)["tools"], self.facts(report)["tool_check"]))
            self.assertEqual("completed-claim", inspect_run(t.repo, self.store, assignment).state)
            shell = [["tool.execution_start", {"toolName": "shell", "arguments": {"command": "rm -rf /"}}]]
            unnamed = [["tool.execution_start", {"arguments": {"path": "@return"}}]]
            elsewhere = [["tool.execution_start", {"toolName": "create", "arguments": {"path": "other.json"}}]]
            viewed = [["tool.execution_start", {"toolName": "view", "arguments": {"path": "@return"}}]]
            for name, events in (("no-return-write", READ), ("outside", READ + WRITE + shell),
                                 ("unnamed", WRITE + unnamed), ("elsewhere", elsewhere), ("view-only", viewed)):
                with self.subTest(name=name):
                    self.failing("tools-" + name, FAKE_TOOL_EVENTS=json.dumps(events))
        start, result = self.records()
        write = {"type": "tool.execution_start", "data": {"toolCallId": "x", "toolName": "create",
                                                          "arguments": {"path": RETURN}}}
        view = {"type": "tool.execution_start", "data": {"toolCallId": "v", "toolName": "view",
                                                         "arguments": {"path": RETURN}}}
        done = {"type": "tool.execution_complete", "data": {"toolCallId": "x", "success": True}}
        self.assertEqual(["create"], self.terminal([start, write, done, result]).facts["tools"])
        self.assertNotIn("tools", self.terminal([start, result]).facts)
        self.unknown([start, view, done, result])  # a view naming the return path is no return write
        for bad in ({"toolName": ""}, {"toolName": 3}, {"toolName": None}, {}):
            with self.subTest(bad=bad):
                self.unknown([start, write, {"type": "tool.execution_start", "data": bad}, result])
        for outcome in (None, {}, {"kind": 3}, "denied"):
            with self.subTest(outcome=outcome):
                self.unknown([start, {"type": "permission.completed", "data": {"requestId": "q", "result": outcome}},
                              result])

    def test_hook_and_subagent_events_are_isolation_gaps(self):
        events = [["hook.start", {"hookInvocationId": "h1", "hookType": "preToolUse"}],
                  ["hook.end", {"hookInvocationId": "h1", "hookType": "preToolUse", "success": True}],
                  ["subagent.started", {"toolCallId": "s1", "agentName": "explore", "agentDisplayName": "Explore",
                                        "agentDescription": "x"}]]
        assignment, report = self.author(FAKE_TOOL_EVENTS=json.dumps(events))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)  # recorded, not a failure
        self.assertEqual((["hook:preToolUse", "subagent:explore"], "blocked"),
                         (self.facts(report)["isolation_gaps"], self.facts(report)["tool_check"]))
        start, result = self.records()
        self.assertEqual(['hook:{"x":1}', 'subagent:{"y":2}'], self.terminal(
            [start, {"type": "hook.progress", "data": {"x": 1}}, {"type": "subagent.failed", "data": {"y": 2}},
             result]).facts["isolation_gaps"])

    def test_return_write_basis_is_a_tool_or_a_shell_command_naming_the_return_path(self):
        """Return-write round, ruling 1 (smoke 3: S3 wrote a complete return with an approved heredoc redirect). The
        return write shows as a create/edit start naming the path (basis "tool") or a `bash` start whose command writes
        exactly that path through `>`, `>>`, `>|`, `&>` or `tee` (basis "shell", fix 1 F2/F3); a start whose own
        completion was denied never counts (fix 1 F1); neither is `unknown` as before."""
        heredoc = ["tool.execution_start", {  # the live S3 shape, command shortened, path substituted
            "toolCallId": "toolu_01Hk18fQBP1tLXCtvi697jT1", "toolName": "bash",
            "arguments": {"command": "cat > @return << 'EOF'\n{\"affected_ids\":[]}\nEOF"}, "turnId": "21",
            "model": "claude-sonnet-5", "toolTitle": "Running command",
            "shellToolInfo": {"possiblePaths": ["@return"], "hasWriteFileRedirection": True}}]
        for name, events, basis in (("tool", WRITE, "tool"), ("shell", [heredoc], "shell"),
                                    ("both", [heredoc] + WRITE, "tool")):
            with self.subTest(basis=name):
                t = self.tree("basis-" + name)
                assignment, report = self.author(t, "basis-" + name, FAKE_TOOL_EVENTS=json.dumps(events))
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                self.assertEqual((basis, "passed"), (self.facts(report)["return_write_basis"],
                                                     self.facts(report)["tool_check"]))
                self.assertEqual("completed-claim", inspect_run(t.repo, self.store, assignment).state)
        start, result = self.records()
        bash = {"type": "tool.execution_start", "data": {"toolCallId": "b", "toolName": "bash",
                                                         "arguments": {"command": None}}}
        P = RETURN
        live = (  # the 5 live S3 commands naming the path (smoke-copilot-3), shortened: only the heredoc wrote it
            (f"cd /w && python3 << 'PYEOF'\nimport json\nout_path = \"{P}\"\nPYEOF", False),
            (f"cat > {P} << 'EOF'\n{{\"affected_ids\":[]}}\nEOF", True),
            (f"RET={P}\ncat \"$RET\" | python3 -c \"import json,sys; json.load(sys.stdin)\" 2>&1", False),
            (f"RET={P}\njq . \"$RET\" > /tmp/out_check.json 2>&1; echo \"exit:$?\"", False),
            (f"RET={P}\njq . \"$RET\" >/dev/null && echo OUTER_OK", False))
        shapes = (  # write shapes (accepted) and reads, copies, variables, comments and suffixes (rejected)
            (f"cat >> {P}", True), (f"echo x >| {P}", True), (f"printf x &> {P}; ls", True),
            (f"echo x | tee {P}", True), (f"echo x | tee -a '{P}'", True), (f"cat >'{P}'<<'EOF'", True),
            (f"cat '{P}'", False), (f"ls -l {P}", False), (f"rm -f {P}", False), (f"true # {P}", False),
            (f"cp /tmp/x {P}", False), (f"mv /tmp/x {P}", False), ("cat > \"$RET\"", False),
            (f"cat > /tmp/x << 'EOF'\n{{\"path\": \"{P}\"}}\nEOF", False),
            (f"cat > {P}.bak", False), (f"cat > {P}x", False), (f"cat > /x{P}", False),
            ("cat > /r/worker-return/other.json", False),
            *((f"cat > {P}{suffix}", False) for suffix in "~:#+@"))
        for command, ok in live + shapes:
            with self.subTest(command=command):
                bash["data"]["arguments"]["command"] = command
                if ok:
                    self.assertEqual("shell", self.terminal([start, bash, result]).facts["return_write_basis"])
                else:
                    self.unknown([start, bash, result])
        self.assertNotIn("return_write_basis", self.terminal([start, result]).facts)  # no tool events: no basis
        self.failing("basis-none", FAKE_TOOL_EVENTS=json.dumps(READ))  # a return file but no matching event
        denied_heredoc = [heredoc, ["tool.execution_complete", {  # fix 1 F1: the live denial shape
            "toolCallId": heredoc[1]["toolCallId"], "success": False, "error": UV_ERROR}]]
        denied_create = WRITE[:1] + [["tool.execution_complete", {"toolCallId": "w1", "success": False,
                                                                   "error": BMAD_ERROR}]]
        idless = [["tool.execution_start", {"toolName": "create", "arguments": {"path": "@return"}}]]  # fail closed
        for name, events in (("shell", denied_heredoc), ("tool", denied_create), ("no-id", idless)):
            with self.subTest(denied=name):  # a present, valid return file does not rescue a denied write
                report = self.failing("denied-" + name, FAKE_TOOL_EVENTS=json.dumps(events))
                self.assertIn("tool events name no write of the return path", report.evidence_needs[0])
        with self.subTest(denied="shell-then-surviving-tool"):  # the denied shell start neither cancels nor beats the create
            t = self.tree("basis-denied-shell-then-tool")
            assignment, report = self.author(t, "basis-denied-shell-then-tool",
                                             FAKE_TOOL_EVENTS=json.dumps(denied_heredoc + WRITE))
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            self.assertEqual(("tool", "passed"), (self.facts(report)["return_write_basis"],
                                                  self.facts(report)["tool_check"]))
            self.assertEqual("completed-claim", inspect_run(t.repo, self.store, assignment).state)

    def test_terminal_refusals_name_their_specific_cause(self):
        """Return-write round, ruling 3: each refusal carries its own cause into the run's evidence needs."""
        unknown_tool = [["tool.execution_start", {"toolName": "web_fetch", "arguments": {}}]]
        offered = [["session.usage_checkpoint", {"promptCacheBreakState": [{"models": {"m": {"tools": [
            {"name": "web_fetch"}]}}}]}]]
        for name, env, cause in (
                ("no-result", {"FAKE_MODE": "no-result"}, "stream lacks exactly one result as its last record"),
                ("truncated", {"FAKE_MODE": "truncated"}, "not one JSON object with a string type"),
                ("session", {"FAKE_MODE": "wrong-session"}, "the result record names another session than the preset"),
                ("exit", {"FAKE_MODE": "exit-code-only"}, "the result record's exitCode is not 0"),
                ("error", {"FAKE_MODE": "session-error"}, "stream reports a session error"),
                ("return", {"FAKE_RETURN": "missing"}, "Worker return file is missing or malformed"),
                ("write", {"FAKE_TOOL_EVENTS": json.dumps(READ)}, "tool events name no write of the return path"),
                ("pin", {"FAKE_TOOL_EVENTS": json.dumps(WRITE + unknown_tool)}, "a tool outside the allowed tools"),
                ("offered", {"FAKE_TOOL_EVENTS": json.dumps(offered)}, "offered a tool outside the pinned author set"),
                ("locks", {"FAKE_LOCKS": "extra"}, "the lock folder holds more or other than the session's lock"),
                ("second", {"FAKE_MODE": "preexisting-session"}, "the session is not the one fresh session"),
                ("early", {"FAKE_MODE": "early-created"}, "the session was created before the spawn")):
            with self.subTest(cause=name):
                report = self.failing("cause-" + name, **env)
                self.assertEqual(1, len(report.evidence_needs))
                self.assertIn(cause, report.evidence_needs[0])
        records = self.records()
        for name, freshness, cause in (  # fix 1 F4: the sealed observation, never blamed on the stream
                ("missing", None, "a retained run file (events.jsonl or freshness.json) is missing or unreadable"),
                ("malformed", b"not json {", "the sealed freshness.json is not one JSON object"),
                ("array", b"[]", "the sealed freshness.json is not one JSON object")):
            with self.subTest(freshness=name):
                directory = self.root / f"fresh-{name}"
                (directory / clients.WORKER_RETURN).mkdir(parents=True)
                (directory / "events.jsonl").write_bytes(b"".join(json.dumps(item).encode() + b"\n"
                                                                 for item in records))
                if freshness is not None:
                    (directory / "freshness.json").write_bytes(freshness)
                launch = {"session_id": SESSION, "worktree": "/w", "return_path": RETURN,
                          "pre_spawn_at": "2026-10-05T10:00:00.123456+00:00", "home_session_preexisted": False}
                refused = self.code("UNKNOWN_EVIDENCE", clients.COPILOT.terminal, directory, launch)
                self.assertEqual("Copilot terminal refused: " + cause, refused.message)
        self.assertEqual("a stream record or the sealed freshness has an unexpected shape",
                         clients._cause(KeyError("data")))  # the fallback for any other shape error

    def test_mcp_connection_is_isolation_gap(self):
        events = [["session.mcp_server_status_changed", {"serverName": "github-mcp-server", "status": "connected"}]]
        assignment, report = self.author(FAKE_TOOL_EVENTS=json.dumps(events))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual((["mcp-server:github-mcp-server"], "blocked"),
                         (self.facts(report)["isolation_gaps"], self.facts(report)["tool_check"]))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        start, result = self.records()
        unnamed = {"type": "session.mcp_server_needs_reconnect", "data": {"status": "x"}}
        self.assertEqual(['mcp-server:{"status":"x"}'], self.terminal([start, unnamed, result]).facts["isolation_gaps"])
        loaded = {"type": "session.mcp_servers_loaded", "data": {"servers": [{"name": "github-mcp-server"}, {"x": 1}]}}
        self.assertEqual(['mcp-server:github-mcp-server', 'mcp-server:{"x":1}'],
                         self.terminal([start, loaded, result]).facts["isolation_gaps"])
        live = {"type": "session.mcp_servers_loaded", "data": {"servers": [  # the P0c shape (smokes 1-3)
            {"name": "github-mcp-server", "status": "disabled", "source": "builtin"},
            {"name": "githubiq", "status": "disabled"}]}}
        self.assertEqual([], self.terminal([start, live, result]).facts["isolation_gaps"])  # return-write ruling 4
        live["data"]["servers"][1]["status"] = "connected"
        self.assertEqual(["mcp-server:githubiq"], self.terminal([start, live, result]).facts["isolation_gaps"])

    def test_usage_and_version_recorded_as_client_facts(self):
        assignment, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual({"usage": USAGE, "copilotVersion": "9.9.9", "denials": [], "isolation_gaps": [],
                          "tool_check": "blocked"}, self.facts(report))
        self.assertEqual(VERSION, self.receipt(report, "launch.json")["cli_version"])  # first line, no update hint
        with patch.dict(os.environ, self.env):
            self.assertEqual((str(self.fake_dir / "copilot"), VERSION), clients.preflight(clients.COPILOT))
        request = {"requestId": "q1", "permissionRequest": {"kind": "shell", "fullCommandText": "git push"}}
        denied = {"requestId": "q1", "result": {"kind": "denied-by-rules"}}
        events = [["permission.requested", request], ["permission.completed", denied],
                  ["permission.requested", {**request, "requestId": "q2"}],
                  ["permission.completed", {"requestId": "q2", "result": {"kind": "approved"}}]]
        t = self.tree("denied")
        assignment, report = self.author(t, "denied", FAKE_TOOL_EVENTS=json.dumps(events))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)  # denials alone change no validity
        self.assertEqual(([denied], "blocked"), (self.facts(report)["denials"], self.facts(report)["tool_check"]))
        self.assertEqual("completed-claim", inspect_run(t.repo, self.store, assignment).state)

    def test_token_variable_names_recorded_never_values(self):
        secrets = {"COPILOT_GITHUB_TOKEN": "github_pat_fake_copilot_value", "GH_TOKEN": "gho_fake_gh_value",
                   "GITHUB_TOKEN": "ghs_fake_github_value", "COPILOT_PARENT_MARKER": "parent-marker-value",
                   "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/private/instructions-dir"}
        out = self.root / "env.json"
        _, report = self.author(FAKE_ENV_OUT=str(out), COPILOT_ALLOW_ALL="true", **secrets)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        isolation = self.receipt(report, "launch.json")["isolation"]
        removed = {"COPILOT_ALLOW_ALL", "COPILOT_PARENT_MARKER", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS"}
        self.assertEqual((list(TOKENS), ["COPILOT_GITHUB_TOKEN"], sorted(removed)),
                         (isolation["token_names"], isolation["kept_names"], isolation["removed_names"]))
        self.assertLessEqual({"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}, set(isolation["inherited_names"]))
        seen = set(json.loads(out.read_text())["names"])
        self.assertLessEqual({*TOKENS, "COPILOT_HOME", "COPILOT_AUTO_UPDATE"}, seen)
        self.assertFalse(removed & seen)
        for path in Path(report.run_dir).rglob("*"):
            if path.is_file() and not path.is_symlink():
                for value in secrets.values():
                    self.assertNotIn(value.encode(), path.read_bytes(), path.name)
        version_env = self.root / "version-env.json"
        with patch.dict(os.environ, {**self.env, **secrets, "COPILOT_ALLOW_ALL": "true",
                                     "FAKE_VERSION_ENV_OUT": str(version_env)}):
            clients.preflight(clients.COPILOT)
        names = set(json.loads(version_env.read_text()))
        self.assertLessEqual({*TOKENS, "COPILOT_AUTO_UPDATE"}, names)
        self.assertFalse(removed & names)

    def test_doctor_reports_copilot_preflight_and_token_warning(self):
        def doctor(*args, **env):
            environ = {**os.environ, **self.env, **env}
            if "PATH" not in env:  # the fake comes first; a real client is never reached
                self.assertEqual(self.fake_dir, Path(shutil.which("copilot", path=environ["PATH"])).parent)
            result = subprocess.run([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"), "--repo",
                                     str(self.t.path), "doctor", "--base", self.base, "--client", COPILOT, *args],
                                    capture_output=True, text=True, env=environ)
            self.assertEqual(0, result.returncode, result.stderr)
            for value in env.values():
                if "secret" in value:
                    self.assertNotIn(value, result.stdout)
            return json.loads(result.stdout)

        absent = doctor()
        warning = absent["client"]["warnings"]
        self.assertEqual({"name": COPILOT, "binary": str(self.fake_dir / "copilot"), "version": VERSION,
                          "ready": True, "reasons": [], "token_names": [], "warnings": warning}, absent["client"])
        self.assertEqual(1, len(warning))
        self.assertIn("COPILOT_GITHUB_TOKEN", warning[0])
        self.assertTrue(absent["ready"])  # a warning, not a readiness failure
        fallback = doctor(GH_TOKEN="gho-secret-value")["client"]
        self.assertEqual((["GH_TOKEN"], warning), (fallback["token_names"], fallback["warnings"]))
        present = doctor(COPILOT_GITHUB_TOKEN="pat-secret-value", GITHUB_TOKEN="ghs-secret-value")["client"]
        self.assertEqual((["COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN"], [], True),
                         (present["token_names"], present["warnings"], present["ready"]))
        failing = doctor(FAKE_VERSION_FAIL="exit")
        self.assertEqual((False, False, None), (failing["client"]["ready"], failing["ready"],
                                                failing["client"]["version"]))
        empty = self.root / "empty-bin"
        empty.mkdir()
        self.assertIsNone(shutil.which("copilot", path=safe_path(empty)))
        missing = doctor(PATH=safe_path(empty))
        self.assertEqual((None, False), (missing["client"]["binary"], missing["ready"]))
        self.assertEqual(4, self.version_count())
        self.assertFalse(self.count.exists())  # a preflight is never a model call or a spawn

    # -- live probe 1 fix round (rulings R1-R6) ----------------------------------------------------------------------

    def recording_uv(self) -> Path:
        """Replace the fake `uv` with one that records each call (one JSON line) to <root>/uv.jsonl."""
        (self.fake_dir / "uv").write_text(UV_RECORDER)
        (self.fake_dir / "uv").chmod(0o755)
        return self.root / "uv.jsonl"

    def uv_records(self, out: Path) -> list:
        return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []

    def uv_calls(self, out: Path) -> list:
        return [item["argv"] for item in self.uv_records(out)]

    def test_script_launcher_runs_only_bound_forms_and_refuses_everything_else(self):
        commands = tuple(planning.author_commands("/w", ".agents/skills", "bmad-prd", "ws/prd"))
        forms = clients.launcher_forms(commands)
        self.assertEqual(((("/w/_bmad/scripts/resolve_customization.py", "--skill", "/w/.agents/skills/bmad-prd"),
                           ("/w/_bmad/scripts/resolve_config.py", "--project-root", "/w"),
                           ("/w/_bmad/scripts/memlog.py", "init", "--workspace", "ws/prd"),
                           ("/w/_bmad/scripts/memlog.py", "append", "--workspace", "ws/prd"))), forms)
        out, worktree = self.recording_uv(), self.root / "launcher-worktree"
        worktree.mkdir()
        tools = {"python": sys.executable, "uv": str(self.fake_dir / "uv"), "path": os.environ["PATH"],
                 "home": str(self.root / "sealed-home")}
        data = clients.launcher_bytes(commands, tools, str(worktree))
        self.assertTrue(data.startswith(f"#!{sys.executable} -IS\n".encode()))  # isolated, no site: no PYTHON* read
        self.assertEqual(data, clients.launcher_bytes(commands, dict(tools), str(worktree)))  # deterministic
        for other in (clients.launcher_bytes(tuple(planning.author_commands(
                          "/w", ".agents/skills", "bmad-prd", "ws/other")), tools, str(worktree)),
                      clients.launcher_bytes(commands, {**tools, "uv": "/elsewhere/uv"}, str(worktree)),
                      clients.launcher_bytes(commands, {**tools, "home": "/elsewhere"}, str(worktree)),
                      clients.launcher_bytes(commands, {**tools, "path": "/elsewhere"}, str(worktree)),
                      clients.launcher_bytes(commands, tools, "/elsewhere")):
            self.assertNotEqual(data, other)
        for over in ({"python": "python3"}, {"python": "/usr/bin/py thon"}, {"python": "/" + "p" * 120},
                     {"python": "/" + "é" * 60}, {"uv": "uv"}, {"home": "home"},  # é: 2 bytes, 121 in all
                     {"python": "/usr/bin/py\udc80"}):  # a non-UTF-8 path: refused, never UnicodeEncodeError
            with self.subTest(over=over):
                self.code("BLOCKED_EVIDENCE", clients.launcher_bytes, commands, {**tools, **over}, str(worktree))
        placeholder = tuple(planning.author_commands("/w/__UV__", ".agents/skills", "bmad-prd", "ws/prd"))
        self.assertIn(b"/w/__UV__/_bmad/scripts/memlog.py", clients.launcher_bytes(placeholder, tools, str(worktree)))
        directory = self.root / "launcher-run"
        directory.mkdir()
        self.assertEqual(sha(data), clients.write_launcher(directory, data))
        launcher = clients.launcher_path(directory)
        self.assertEqual(directory / "worker-tools" / "bmad-script", launcher)
        self.assertEqual((data, 0o500, 0o700), (launcher.read_bytes(), launcher.stat().st_mode & 0o777,
                                                launcher.parent.stat().st_mode & 0o777))
        with self.assertRaises(FileExistsError):  # one launcher per run, never rewritten
            clients.write_launcher(directory, data)
        # Fix rounds 2-3 (I1, I3): a worker-planted sitecustomize on PYTHONPATH, a shadow `uv` first on PATH, an
        # env-prefixed HOME, XDG_*, LD_*, UV_*, PYTHON* or SSL variable never takes effect: -I ignores PYTHON*, uv is
        # the sealed absolute path, and its environment is built from the sealed PATH and HOME plus the token names.
        planted, marker = self.root / "planted", self.root / "planted-ran"
        planted.mkdir()
        (planted / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'a').write('ran\\n')\n")
        (planted / "uv").write_text(f"#!/bin/sh\necho shadow >> {marker}\n")
        (planted / "uv").chmod(0o755)
        env = {**os.environ, "PATH": str(planted) + os.pathsep + os.environ["PATH"], "PYTHONPATH": str(planted),
               "PYTHONSTARTUP": str(planted / "sitecustomize.py"), "PYTHONHOME": "/x", "UV_PYTHON": "/evil/python",
               "UV_CONFIG_FILE": "/evil/uv.toml", "VIRTUAL_ENV": "/evil", "CONDA_PREFIX": "/evil",
               "PYTHONDONTWRITEBYTECODE": "0", "HOME": "/evil-home", "XDG_CONFIG_HOME": "/evil",
               "XDG_CACHE_HOME": "/evil", "XDG_DATA_HOME": "/evil", "LD_LIBRARY_PATH": "/evil",
               "SSL_CERT_FILE": "/evil", "GH_TOKEN": "gho-fake-token", "COPILOT_GITHUB_TOKEN": "pat-fake-token",
               "GITHUB_TOKEN": "ghs-fake-token"}  # security round: no token reaches uv or the script
        bare = {"PATH": os.environ["PATH"], "PYTHONPATH": str(planted)}  # the test caveat: no PYTHONHOME here

        def launch(*args, environ=None):
            before = len(self.uv_records(out))
            done = subprocess.run([str(launcher), *args], capture_output=True, text=True, env=environ or env,
                                  cwd=self.root)
            return done, self.uv_records(out)[before:]

        def call(args):
            return {"argv": ["run", "--no-config", "--no-project", "--python", sys.executable, *args],
                    "cwd": str(worktree), "names": ["HOME", "PATH", "PYTHONDONTWRITEBYTECODE"], "path": tools["path"],
                    "home": tools["home"], "bytecode": "1"}

        trailing = ("--project-root", "/w", "--key", "workflow")
        for args in (*forms, forms[0] + trailing, forms[3] + ("--entry", "a b; rm -rf / $(x)"),
                     forms[3] + ("--", "--workspace-like text"), forms[3] + ("--text", "- a bullet", "--by", "-1")):
            with self.subTest(allowed=args):
                done, calls = launch(*args)
                self.assertEqual((0, [call(args)]), (done.returncode, calls), done.stderr)
        for args, code in ((forms[2], 0), (("/bin/sh", "-c", "true"), 2)):  # PYTHONPATH alone: only -I stops it
            with self.subTest(sitecustomize=args):
                done, calls = launch(*args, environ=bare)
                self.assertEqual((code, int(not code)), (done.returncode, len(calls)), done.stderr)
                self.assertFalse(marker.exists())
        memlog = "/w/_bmad/scripts/memlog.py"
        for args in ((), (memlog,), (memlog, "init"), (memlog, "init", "--workspace"),
                     (memlog, "init", "--workspace", "other"), (memlog, "init", "--workspace", "ws/prd2"),
                     (memlog, "init", "--workspace", "ws/prd/../x"), (memlog, "--workspace", "ws/prd", "init"),
                     ("_bmad/scripts/memlog.py", "init", "--workspace", "ws/prd"),
                     (memlog + " init", "--workspace", "ws/prd"), (memlog, "init", "--workspace ws/prd"),
                     ("uv", "run", "--no-project", memlog, "init", "--workspace", "ws/prd"),
                     (memlog, "init", "--verbose", "--workspace", "ws/prd"), ("/w/_bmad/scripts/other.py",),
                     ("--", "/w/_bmad/scripts/resolve_config.py", "--project-root", "/w"),
                     ("/w/_bmad/scripts/resolve_config.py", "--project-root=/w"),
                     ("/w/_bmad/scripts/resolve_config.py", "--project-root", "/W"), ("/bin/sh", "-c", "true"),
                     # M1: a trailing argument may not repeat or abbreviate an option of the form (argparse keeps
                     # the last value and accepts unique prefixes)
                     forms[2] + ("--workspace", "/elsewhere"), forms[2] + ("--work", "/elsewhere"),
                     forms[3] + ("--workspace=/elsewhere",), forms[3] + ("--w=/x",),
                     forms[1] + ("--project-root", "/x"), forms[0] + ("--skill=/x",), forms[0] + ("--sk", "/x"),
                     # fix round 3: no short option either (resolve_config -p, resolve_customization -s/-p)
                     forms[1] + ("-p", "/x"), forms[0] + ("-s", "/x"), forms[0] + ("-pfoo",), forms[3] + ("-x",)):
            with self.subTest(refused=args):
                done, calls = launch(*args)
                self.assertEqual((2, []), (done.returncode, calls))
                self.assertIn("bmad-script: refused, nothing was run", done.stderr)
                self.assertIn("bmad-script /w/_bmad/scripts/memlog.py init --workspace ws/prd ...", done.stderr)
        self.assertFalse(marker.exists())  # no planted code ran, before or after validation
        for bad in ((), ("uv run /w/_bmad/scripts/memlog.py init",), ("uv run --no-project  /w/x.py",),
                    ("uv run --no-project _bmad/scripts/memlog.py init",), ("uv run --no-project /w/memlog",),
                    ("uv run --no-project /w/x.py ",), ("sh -c true",)):
            with self.subTest(malformed=bad):
                self.code("INVALID_INPUT", clients.launcher_forms, bad)
        with patch.dict(os.environ, self.env):  # the coordinator's PATH, with the fakes first
            self.assertEqual({"python": sys.executable, "uv": str(self.fake_dir / "uv"), "path": self.env["PATH"],
                              "home": self.env["HOME"]}, clients.launcher_tools())
        empty = self.root / "no-uv"
        empty.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty)}):
            self.code("BLOCKED_EVIDENCE", clients.launcher_tools)

    def test_worker_planted_uv_toml_never_affects_a_launcher_run(self):
        self.assertIsNotNone(REAL_UV, "Test prerequisite: a real `uv` on PATH (as the coordinator itself needs it for "
                                      "a Copilot bound author); install uv to run this suite")
        worktree, home = self.root / "uv-worktree", self.root / "uv-home"
        (worktree / "_bmad" / "scripts").mkdir(parents=True)
        home.mkdir()
        script = worktree / "_bmad" / "scripts" / "probe.py"
        script.write_text('# /// script\n# requires-python = ">=3.10"\n# dependencies = []\n# ///\nprint("probe-ok")\n')
        tools = {"python": sys.executable, "uv": REAL_UV, "path": os.environ["PATH"], "home": str(home)}
        directory = self.root / "uv-run"
        directory.mkdir()
        clients.write_launcher(directory, clients.launcher_bytes((f"{UV}{script}",), tools, str(worktree)))
        cache = worktree / "planted-cache"
        configs = (("malformed", "not = = toml\n"), ("redirecting", f'cache-dir = "{cache}"\n'))
        for name, text in configs:
            with self.subTest(config=name):  # fix round 3 (I3): `--no-config`, so the author's uv.toml is not read
                (worktree / "uv.toml").write_text(text)
                done = subprocess.run([str(clients.launcher_path(directory)), str(script)], capture_output=True,
                                      text=True, cwd=self.root, env={"PATH": os.environ["PATH"]}, timeout=120)
                self.assertEqual((0, "probe-ok\n"), (done.returncode, done.stdout), done.stderr)
                self.assertFalse(cache.exists())
        controls = []  # the controls: the same files do change a plain `uv run --no-project`
        for name, text in configs:
            (worktree / "uv.toml").write_text(text)
            controls.append(subprocess.run([REAL_UV, "run", "--no-project", str(script)], capture_output=True,
                                           text=True, cwd=worktree, env={"PATH": os.environ["PATH"], "HOME": str(home)},
                                           timeout=120).returncode)
        self.assertEqual((True, True), (controls[0] != 0, cache.is_dir()))

    def test_worktree_venv_never_supplies_the_launcher_run_interpreter(self):
        """Security round (I1): uv finds a `.venv` in the worktree or a parent and, on a fresh cache, runs its
        interpreter, whose site `.pth` files the author can write (gitignored, so no inventory delta). The launcher
        seals `--python <coordinator interpreter>`, so uv never discovers the venv (a coordinator interpreter under the
        worktree is refused separately, m1); the control shows the planted `.pth` does run without that flag."""
        self.assertIsNotNone(REAL_UV, "Test prerequisite: a real `uv` on PATH (as the coordinator itself needs it for "
                                      "a Copilot bound author); install uv to run this suite")
        worktree, marker = self.root / "venv-worktree", self.root / "pth-ran"
        (worktree / "_bmad" / "scripts").mkdir(parents=True)
        script = worktree / "_bmad" / "scripts" / "probe.py"
        script.write_text('# /// script\n# requires-python = ">=3.10"\n# dependencies = []\n# ///\nprint("probe-ok")\n')
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(worktree / ".venv")], check=True,
                       capture_output=True, timeout=120)
        site = worktree / ".venv" / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
        (site / "zz_planted.pth").write_text(f"import os; open({str(marker)!r}, 'a').write('pth\\n')\n")

        def fresh_home(name):  # a fresh uv cache each time: the case where the planted .pth ran
            home = self.root / name
            home.mkdir()
            return str(home)

        tools = {"python": sys.executable, "uv": REAL_UV, "path": os.environ["PATH"], "home": fresh_home("venv-home")}
        directory = self.root / "venv-run"
        directory.mkdir()
        clients.write_launcher(directory, clients.launcher_bytes((f"{UV}{script}",), tools, str(worktree)))
        done = subprocess.run([str(clients.launcher_path(directory)), str(script)], capture_output=True, text=True,
                              cwd=self.root, env={"PATH": os.environ["PATH"]}, timeout=120)
        self.assertEqual((0, "probe-ok\n", False), (done.returncode, done.stdout, marker.exists()), done.stderr)
        control = subprocess.run([REAL_UV, "run", "--no-config", "--no-project", str(script)], capture_output=True,
                                 text=True, cwd=worktree, timeout=120,
                                 env={"PATH": os.environ["PATH"], "HOME": fresh_home("control-home")})
        self.assertEqual((0, True), (control.returncode, marker.exists()), control.stderr)  # the round-3 argv

    def test_a_literally_pasted_skeleton_is_refused_for_every_profile(self):
        """Return-schema round review, minor 2: the skeleton is a shape, not a return. Pasted as it is, with the id
        placeholders or the envelope's real ids, `_final` refuses it for the generic, planning and review profiles."""
        row = {"id": str(uuid.uuid4()), "request_fingerprint": "f" * 64}
        for profile in (worker.profiles.GENERIC, planning.PLANNING_PROFILE, review.REVIEW_PROFILE):
            schema = worker._schema(profile)
            for ids in (None, {"assignment_id": row["id"], "request_fingerprint": row["request_fingerprint"]}):
                with self.subTest(profile=profile.name, ids=bool(ids)):
                    pasted = self.skeleton(worker.return_schema_text(schema, ids))
                    self.assertEqual(sorted(schema["required"]), sorted(pasted))
                    self.code("UNKNOWN_EVIDENCE", worker._final, pasted, row, profile)
        shaped = self.skeleton(worker.return_schema_text(worker._schema()))  # without ids: the brief's shapes
        self.assertEqual(("<uuid4>", "<sha256 hex>"), (shaped["assignment_id"], shaped["request_fingerprint"]))

    def test_interpreter_under_the_worktree_or_common_dir_is_refused_before_reservation(self):
        """Followups review m1: a coordinator run from a venv inside the feature worktree or its common directory would
        hand uv a worker-writable interpreter (`.pth`, `pyvenv.cfg`); `launcher_tools` refuses it before reservation."""
        self.planning()  # the fakes (uv among them) first on PATH from here on
        roots = (self.t.path, self.t.repo.common_dir)
        link = self.root / "python-link"
        os.symlink(self.t.path / "bin-python", link)  # outside by name, inside by target
        real = self.root / "real-checkout"  # a checkout reached through a linked parent (~/repos -> /data/repos)
        (real / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, real / ".venv" / "bin" / "python")  # a venv python links to its base interpreter
        os.symlink(real, self.root / "linked-checkout")
        for python, tops in ((str(self.t.path / ".venv" / "bin" / "python"), roots),
                             (str(self.t.repo.common_dir / "py" / "python3"), roots), (str(link), roots),
                             (str(self.root / "linked-checkout" / ".venv" / "bin" / "python"), (real,))):
            with self.subTest(python=python), patch.object(clients.sys, "executable", python):
                refused = self.code("BLOCKED_EVIDENCE", clients.launcher_tools, tops)
                self.assertIn("under the worktree or common directory", refused.message)
        self.assertEqual(sys.executable, clients.launcher_tools(roots)["python"])  # the test interpreter is outside
        assignment = self.prep("p1", client=COPILOT)["assignment_id"]
        versions = self.version_count()
        with patch.object(clients.sys, "executable", str(self.t.path / ".venv" / "bin" / "python")):
            self.code("BLOCKED_EVIDENCE", self.run_it, assignment)
        self.assertEqual(("prepared", [], versions), (self.state(assignment), self.reserved(assignment),
                                                      self.version_count()))  # no attempt, no preflight
        self.assertFalse(os.path.lexists(self.run_dir(assignment)))

    def test_bound_copilot_author_gets_the_launcher_rule_pinned_tools_envelope_forms_and_sealed_digest(self):
        self.planning()
        prepared = self.prep("p1", client=COPILOT)
        assignment = prepared["assignment_id"]
        commands = self.binding_of(assignment)["commands"]  # the binding keeps the uv forms (no migration)
        report = self.planned(assignment)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory, argv = Path(report.run_dir), self.argv(report)
        launcher = directory / "worker-tools" / "bmad-script"
        rules = [argv[index + 1] for index, item in enumerate(argv) if item == "--allow-tool"]
        self.assertEqual(["write", "shell(bmad-script)", "shell(sha256sum)"], rules)
        self.assertFalse([item for item in argv if "uv run" in item])
        self.assertEqual(",".join(sorted(AUTHOR_TOOLS)), argv[argv.index("--available-tools") + 1])
        data, uv = launcher.read_bytes(), str(self.fake_dir / "uv")  # uv: resolved on the coordinator's PATH
        self.assertEqual(clients.launcher_bytes(tuple(commands), clients.launcher_tools(), str(self.t.path)), data)
        self.assertEqual(0o500, launcher.stat().st_mode & 0o777)
        launch = self.receipt(report, "launch.json")
        self.assertEqual({"path": str(launcher), "sha256": sha(data), "rule": "shell(bmad-script)",
                          "path_prepend": str(launcher.parent), "python": sys.executable, "uv": uv},
                         launch["script_launcher"])
        check = directory / clients.LAUNCHER_CHECK  # fix round 2 (I2): sealed after the drain, anchored by digest
        self.assertEqual({"entries": ["bmad-script"], "regular": True, "mode": 0o500, "links": 1, "sha256": sha(data)},
                         json.loads(check.read_bytes()))
        self.assertEqual(sha(check.read_bytes()), self.receipt(report)["launcher_check_sha256"])
        self.assertIn("PATH", launch["env_names"])  # the fake asserts the directory is first on the spawn PATH
        envelope = json.loads((directory / "envelope.json").read_bytes())
        self.assertNotIn("script_launcher", envelope)  # return-schema ruling 2: the bare name is the only form
        schema = json.loads((directory / "return.schema.json").read_bytes())
        self.assertEqual(worker._schema(planning.PLANNING_PROFILE), schema)
        prefix = worker.INSTRUCTIONS + " " + worker.RETURN_FILE.format(envelope["return_path"]) + " " + listing(
            PLANNING_KEYS, planning_skeleton(envelope["assignment_id"], envelope["request_fingerprint"])) + " "
        self.assertTrue(envelope["instructions"].startswith(prefix))
        self.assertEqual(sorted(schema["required"]), sorted(self.skeleton(envelope["instructions"])))  # ruling 1
        self.assertLessEqual({"workflow_json", "customization"}, set(schema["required"]))  # the profile extras
        for command in commands:
            self.assertIn(f"`bmad-script {command[len(UV):]} …`", envelope["instructions"])
            self.assertNotIn(f"`{launcher} ", envelope["instructions"])
        self.assertNotIn("`uv run", envelope["instructions"])
        baseline = clients.sealed_evidence(self.t.repo, "another-run")  # a later run's D16 baseline covers it
        self.assertEqual(sha(data), baseline[launcher.relative_to(self.base_dir).as_posix()])
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        _, generic = self.author(self.tree("generic"), "generic")  # no commands: no launcher, no rule, no field
        self.assertEqual("completed-claim", generic.state, generic.evidence_needs)
        self.assertNotIn("script_launcher", self.receipt(generic, "launch.json"))
        self.assertNotIn("PATH", self.receipt(generic, "launch.json")["env_names"])
        self.assertFalse((Path(generic.run_dir) / "worker-tools").exists())
        self.assertFalse((Path(generic.run_dir) / clients.LAUNCHER_CHECK).exists())
        self.assertNotIn("launcher_check_sha256", self.receipt(generic))
        original = check.read_bytes()  # replay rechecks the sealed result
        check.write_bytes(original.replace(b'"mode":320', b'"mode":448'))
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        check.write_bytes(original)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_launcher_run_of_a_modified_script_is_unknown_through_contract_digests(self):
        self.planning()
        out, launched = self.recording_uv(), self.root / "launched.json"
        assignment = self.prep("p1", client=COPILOT)["assignment_id"]
        binding = self.binding_of(assignment)
        script = f"{binding['worktree']}/_bmad/scripts/memlog.py"
        form = [script, "init", "--workspace", binding["workspace"]]
        report = self.planned(assignment, FAKE_OUTSIDE_WRITE=json.dumps([script]),
                              FAKE_LAUNCH=json.dumps([["bmad-script", *form], ["@launcher", *form],
                                                      ["@launcher", "/bin/sh", "-c", "touch pwned"]]),
                              FAKE_LAUNCH_OUT=str(launched))
        self.held(report, assignment)
        self.assertIn("Immutable source or contract bytes changed", report.evidence_needs[0])
        self.assertEqual([0, 0, 2], [code for code, _ in json.loads(launched.read_text())])  # bare, absolute, refused
        self.assertEqual([["run", "--no-config", "--no-project", "--python", sys.executable, *form]] * 2,
                         [call for call in self.uv_calls(out) if call != ["--version"]])  # (doctor's preflight)
        # the launcher ran the rewritten script; the contract digests caught it
        self.assertFalse((self.t.path / "pwned").exists())

    def launcher_tampered(self, **env):
        """A bound Copilot author whose fake changes RUN/worker-tools during the run (fix round 2, I2)."""
        self.planning()
        assignment = self.prep("p1", client=COPILOT)["assignment_id"]
        tools = self.run_dir(assignment) / "worker-tools"
        report = self.planned(assignment, **{key: value.replace("@tools", str(tools)) for key, value in env.items()})
        self.held(report, assignment)
        self.assertEqual(["Script launcher changed during the run"], report.evidence_needs)
        self.assertFalse(os.path.lexists(Path(report.run_dir) / "return.json"))  # no terminal receipt was sealed
        return json.loads((Path(report.run_dir) / clients.LAUNCHER_CHECK).read_bytes())

    def test_launcher_modified_during_the_run_is_unknown_with_the_fence_held(self):
        state = self.launcher_tampered(FAKE_TAMPER=json.dumps([["modify", "@tools/bmad-script"]]))
        self.assertEqual((["bmad-script"], 0o600), (state["entries"], state["mode"]))  # sealed for the record

    def test_extra_file_beside_the_launcher_is_unknown_with_the_fence_held(self):
        state = self.launcher_tampered(FAKE_RUN_FILE="@tools/uv")  # e.g. a shadow `uv` first on PATH
        self.assertEqual((["bmad-script", "uv"], 0o500), (state["entries"], state["mode"]))
        directory = self.root / "states"
        (directory / "worker-tools").mkdir(parents=True)
        (directory / "elsewhere").write_text("x")
        os.symlink(directory / "elsewhere", directory / "worker-tools" / "bmad-script")
        self.assertEqual({"entries": ["bmad-script"], "regular": None, "mode": None, "links": None, "sha256": None},
                         clients.launcher_state(directory))  # a symlinked launcher is never followed
        self.assertFalse(clients.launcher_intact(clients.launcher_state(directory), sha(b"x")))
        self.assertFalse(clients.launcher_intact(clients.launcher_state(self.root / "absent"), None))

    def test_session_operation_lock_dir_beside_the_session_is_accepted_and_sealed_and_nothing_else_extra(self):
        assignment, report = self.author(FAKE_LOCKS="own")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        session = self.receipt(report, "launch.json")["session_id"]
        fresh = json.loads((Path(report.run_dir) / "freshness.json").read_bytes())
        self.assertEqual(([clients.COPILOT_LOCKS, session], [session + ".lock"]), (fresh["sessions"], fresh["locks"]))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        for kind in ("extra", "foreign", "empty", "file", "other-dot"):
            with self.subTest(kind=kind):
                self.failing("locks-" + kind, FAKE_LOCKS=kind)
        records, locked, lock = self.records(), [clients.COPILOT_LOCKS, SESSION], SESSION + ".lock"
        self.assertEqual(SESSION, self.terminal(records, self.fresh(sessions=locked, locks=[lock])).thread_id)
        for name, fresh in (("no-locks-key", self.fresh(sessions=locked)),
                            ("locks-without-dir", self.fresh(locks=[lock])),
                            ("unreadable", self.fresh(sessions=locked, locks=None)),
                            ("two", self.fresh(sessions=locked, locks=[lock, "x.lock"])),
                            ("foreign", self.fresh(sessions=locked, locks=["x.lock"])),
                            ("bare-id", self.fresh(sessions=locked, locks=[SESSION])),
                            ("other-dot", self.fresh(sessions=[".other", SESSION])),
                            ("unsorted", self.fresh(sessions=[SESSION, clients.COPILOT_LOCKS], locks=[lock]))):
            with self.subTest(name=name):
                self.unknown(records, fresh)

    def test_denials_come_from_tool_execution_complete_denied_records_with_permission_fallback(self):
        start, result = self.records()
        write = {"type": "tool.execution_start", "data": {"toolCallId": "w", "toolName": "create",
                                                          "arguments": {"path": RETURN, "file_text": "{}"}}}
        live = [BMAD_START, BMAD_DENIED, UV_START, UV_DENIED, GIT_START, GIT_DONE, write]
        terminal = self.terminal([*live, result])  # the live stream has no session.start
        self.assertEqual([{"toolCallId": "toolu_01PoQmjKFhN2Njfj7e9znqEm", "toolName": "create", "error": BMAD_ERROR},
                          {"toolCallId": "toolu_01HnGGzxhDbRbK1GcBeheKCu", "toolName": "bash", "error": UV_ERROR}],
                         terminal.facts["denials"])
        self.assertEqual((["bash", "create"], None), (terminal.facts["tools"], terminal.facts["copilotVersion"]))
        self.assertEqual("passed", clients.tool_check(clients.COPILOT, "workspace-write", terminal.facts))
        failed = {"type": "tool.execution_complete", "data": {"toolCallId": "f", "success": False,
                                                              "error": {"message": "boom", "code": "failure"}}}
        orphan = {"type": "tool.execution_complete", "data": {"success": False, "error": {"code": "denied"}}}
        for name, extra, expected in (("failure", [failed], []),
                                      ("orphan", [orphan], [{"toolCallId": None, "toolName": None,
                                                             "error": {"code": "denied"}}]),
                                      ("no-error", [{"type": "tool.execution_complete",
                                                     "data": {"success": False}}], []),
                                      ("string-error", [{"type": "tool.execution_complete",
                                                         "data": {"success": False, "error": "denied"}}], [])):
            with self.subTest(name=name):
                self.assertEqual(expected, self.terminal([start, write, *extra, result]).facts["denials"])
        legacy = {"requestId": "q", "result": {"kind": "denied-by-rules"}}  # the permission.* fallback stays
        self.assertEqual([legacy], self.terminal([start, write, {"type": "permission.completed", "data": legacy},
                                                  result]).facts["denials"])
        events = [[item["type"], item["data"]] for item in (BMAD_START, BMAD_DENIED, UV_START, UV_DENIED)] + WRITE
        assignment, report = self.author(FAKE_TOOL_EVENTS=json.dumps(events))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)  # denials alone change no validity
        self.assertEqual((["toolu_01PoQmjKFhN2Njfj7e9znqEm", "toolu_01HnGGzxhDbRbK1GcBeheKCu"], "passed"),
                         ([item["toolCallId"] for item in self.facts(report)["denials"]],
                          self.facts(report)["tool_check"]))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_internal_tools_are_the_narrowed_p0c_vocabulary_pinned_with_available_tools_and_others_unknown(self):
        self.assertEqual((21, 10, 11), (len(set(P0C_TOOLS)), len(set(EXCLUDED) & set(P0C_TOOLS)), len(AUTHOR_TOOLS)))
        self.assertEqual((frozenset(AUTHOR_TOOLS), frozenset(AUTHOR_TOOLS)),
                         (clients.COPILOT_TOOLS, clients.COPILOT.internal_tools))
        argv = clients.COPILOT.argv(self.launch_spec())
        self.assertEqual((1, ",".join(sorted(AUTHOR_TOOLS))),
                         (argv.count("--available-tools"), argv[argv.index("--available-tools") + 1]))
        bash = [["tool.execution_start", {"toolCallId": "b", "toolName": "bash", "arguments": {"command": "ls"}}]]
        assignment, report = self.author(FAKE_TOOL_EVENTS=json.dumps(READ + WRITE + bash))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual((["bash", "create", "view"], "passed"),
                         (self.facts(report)["tools"], self.facts(report)["tool_check"]))
        for name in (*EXCLUDED, "web_search", "Bash", "shell", "github-mcp-server-get_me"):
            with self.subTest(tool=name):
                unknown = [["tool.execution_start", {"toolName": name, "arguments": {}}]]
                self.failing("tool-" + name, FAKE_TOOL_EVENTS=json.dumps(WRITE + unknown))

    def test_offered_tools_from_the_usage_checkpoint_must_lie_in_the_pinned_set(self):
        def checkpoint(names, **over):  # the P0c record shape (smoke-copilot-1 events.jsonl line 648), trimmed
            tools = [{"name": name, "schema_hash": "0" * 12, "safe": True} for name in names]
            return {"type": "session.usage_checkpoint", "data": {"totalPremiumRequests": 1, "promptCacheBreakState": [
                {"models": {"claude-sonnet-5": {"model": "claude-sonnet-5", "tools": tools, **over}}}]}}

        start, result = self.records()
        facts = self.terminal([start, checkpoint(AUTHOR_TOOLS), result]).facts
        self.assertEqual(sorted(AUTHOR_TOOLS), facts["offered_tools"])  # fix round 2 (M3): the pin, checked
        self.unknown([start, checkpoint(P0C_TOOLS), result])  # the unpinned P0c set: the pin did not hold
        self.unknown([start, checkpoint(["view"]), checkpoint(["web_fetch"]), result])
        for malformed in ({"type": "session.usage_checkpoint", "data": {"promptCacheBreakState": "x"}},
                          {"type": "session.usage_checkpoint", "data": {"promptCacheBreakState": [{"models": []}]}},
                          checkpoint([], tools="x"), checkpoint([3])):
            with self.subTest(malformed=malformed):
                self.assertNotIn("offered_tools", self.terminal([start, malformed, result]).facts)

    def test_copilot_model_is_a_required_explicit_parameter_recorded_in_argv_and_launch(self):
        for index, model in enumerate(("", "  ", None, "-m", 5)):
            assignment = self.assign(idempotency_key=f"model-{index}")
            with self.subTest(model=model), patch.dict(os.environ, self.env):
                self.code("INVALID_INPUT", worker.run_assignment, self.t.repo, self.store, self.owner, assignment,
                          model, 5, COPILOT)
                self.assertEqual(("prepared", []), (self.state(assignment), self.reserved(assignment)))
        cli = self.assign(idempotency_key="model-cli")
        self.cli("worker", "run", cli, "--token-file", str(self.token), "--timeout", "5", "--client", COPILOT, code=2)
        self.assertEqual(("prepared", []), (self.state(cli), self.reserved(cli)))
        assignment = self.assign(idempotency_key="model-ok")
        with patch.dict(os.environ, {**self.env, "FAKE_WRITE": writes(("result.md", "m"))}):
            report = worker.run_assignment(self.t.repo, self.store, self.owner, assignment, "claude-sonnet-5", 5,
                                           COPILOT)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        launch = self.receipt(report, "launch.json")
        self.assertEqual(("claude-sonnet-5", "claude-sonnet-5"),
                         (launch["model"], launch["argv"][launch["argv"].index("--model") + 1]))

    def test_worktree_project_skills_are_a_recorded_launch_isolation_gap(self):
        _, report = self.author(self.tree("no-skills"), "no-skills")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual([], self.receipt(report, "launch.json")["isolation"]["gaps"])
        self.planning()
        report = self.planned(self.prep("p1", client=COPILOT)["assignment_id"])
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual(["project-skills:.agents/skills"], self.receipt(report, "launch.json")["isolation"]["gaps"])
