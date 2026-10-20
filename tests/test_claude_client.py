"""Claude Code transport (plan Task 2, design sections 5-9): argv and isolation, the preset session, the stream terminal
rule, client facts, the reviewer Bash audit, briefs and doctor. Every launch runs the fake `claude`; nothing here is
evidence about the live CLI."""

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from coordinator_core import bmad_implementation as impl
from coordinator_core import bmad_planning as planning
from coordinator_core import clients, worker
from coordinator_core.run_receipt import canonical
from coordinator_core.worker import historical_return, inspect_run, reconcile_run
from tests.fake_clients import FORBIDDEN, Harness, golden_normalize, safe_path
from tests.test_bmad_core import PLAN, git, writes
from tests.test_bmad_implementation import BASE_TEST, GREET, PROJECT, SPEC
from tests.test_bmad_planning import MEMLOG, PRD_FILE, WS, flow, paths, question_text

GOLDEN = Path(__file__).parent / "fixtures" / "codex-322752a"
CLAUDE = "claude-code"
AUTHOR = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
SESSION = "7d3f8a52-0c1e-4b8e-9a51-2f6c1d0e4b7a"
ABSENT = object()
VERSION = "9.9.9 (Claude Code fake)"
CLIENT_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
              "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
DENIED = [f"{tool}({path})" for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit")
          for path in ("_bmad/**", ".agents/**", ".claude/**", ".bmad-coordinator/**", "AGENTS.md", "CLAUDE.md")]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ClaudeClientTests(Harness):
    CLIENT = CLAUDE

    # -- helpers -------------------------------------------------------------------------------------------------

    def author(self, t=None, key="one", **env):
        assignment = self.assign(t, idempotency_key=key)
        return assignment, self.go(assignment, t, **{"FAKE_WRITE": writes(("result.md", key)), **env})

    def held(self, report, assignment):
        self.assertEqual("unknown", report.state, report.evidence_needs)
        self.assertEqual("running", self.state(assignment))

    def failing(self, name, **env):
        t = self.tree(name)
        assignment = self.assign(t, idempotency_key=name)
        report = self.go(assignment, t, **env)
        self.held(report, assignment)
        return report

    def launch_spec(self, mode="workspace-write", **over):
        return replace(clients.LaunchSpec("/bin/claude", mode, Path("/w"), Path("/r"), {"type": "object"}, "sonnet",
                                          60, SESSION, (), ()), **over)

    def direct(self, mode="workspace-write", **over):
        """A launch receipt as the runner records it, for direct terminal-rule calls."""
        spec = self.launch_spec(mode, **over)
        launch = {"argv": clients.CLAUDE.argv(spec), "session_id": SESSION, "requested_mode": mode}
        launch["cwd" if spec.isolated else "worktree"] = "/w"
        return launch

    def records(self, launch, bash=(), init=None, result=None):
        """A well-formed stream in the fake's protocol for a recorded launch; ABSENT removes an init key."""
        argv, session = launch["argv"], launch["session_id"]
        first = {"type": "system", "subtype": "init", "session_id": session,
                 "cwd": launch.get("cwd", launch.get("worktree")), "model": "claude-fake-1",
                 "permissionMode": argv[argv.index("--permission-mode") + 1],
                 "tools": argv[argv.index("--tools") + 1].split(","), "mcp_servers": []}
        for key, value in (init or {}).items():
            if value is ABSENT:
                first.pop(key)
            else:
                first[key] = value
        middle = []
        for index, (command, failed) in enumerate(bash):
            tool = f"toolu_{index}"
            middle += [{"type": "assistant", "session_id": session, "message": {"role": "assistant", "content": [
                           {"type": "tool_use", "id": tool, "name": "Bash", "input": {"command": command}}]}},
                       {"type": "user", "session_id": session, "message": {"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": tool, "content": "x", "is_error": failed}]}}]
        last = {"type": "result", "subtype": "success", "is_error": False, "session_id": session, "num_turns": 2,
                "structured_output": {"ok": True}, **(result or {})}
        return [first, *middle, last]

    def stream(self, records):
        directory = self.root / f"stream-{uuid.uuid4().hex}"
        directory.mkdir()
        (directory / "events.jsonl").write_bytes(b"".join(
            (item if isinstance(item, bytes) else json.dumps(item).encode()) + b"\n" for item in records))
        return directory

    def terminal(self, launch, records):
        return clients.CLAUDE.terminal(self.stream(records), launch)

    def unknown(self, launch, records):
        self.code("UNKNOWN_EVIDENCE", self.terminal, launch, records)

    def facts(self, report) -> dict:
        return self.receipt(report)["client_facts"]

    def argv(self, report) -> list:
        return self.receipt(report, "launch.json")["argv"]

    def isolated(self, name, **env):
        root = self.root / "isolated"
        root.mkdir(exist_ok=True)
        cwd = root / (name + "-cwd")
        cwd.mkdir()
        subprocess.run(["git", "-C", str(cwd), "init", "-q"], check=True)
        envelope = {"role": "judge", "assignment_id": "judge-" + name, "request_fingerprint": "f" * 64}
        with patch.dict(os.environ, {**self.env, **env}):
            self.assert_fakes()
            return clients.run_isolated(CLAUDE, self.t.repo, root, cwd, root / (name + "-receipts"), envelope,
                                        worker._schema(), "fake-model", 5)

    def story_stop(self):
        """Design D25: a Claude story attempt (`bmad-implementation-v1`) cut off at its turn limit after writing a file,
        on a story feature added from a committed spec; returns (story repo, assignment id, report)."""
        spec_path = "_bmad-output/implementation-artifacts/spec-1-1-greeting.md"
        for rel, text in ((spec_path, SPEC), ("pkg/__init__.py", ""), ("tests/__init__.py", ""),
                          ("tests/test_base.py", BASE_TEST)):
            (self.main / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.main / rel).write_text(text)
        git(self.main, "add", "-A")
        git(self.main, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "story base")
        with patch.dict(os.environ, {**self.env, "FAKE_MODE": "max-turns", "FAKE_WRITE": writes(("pkg/greet.py", GREET))}):
            found = impl.story_add(self.t.repo, self.store, self.owner, impl.StoryInput(
                spec_path, "1-1-greeting", git(self.main, "rev-parse", "HEAD"), str(self.root / "story"),
                ["pkg/", "tests/"], ["The coordinator commits; never ask whether to commit."]))
            repo = impl.resolve_repo(self.root / "story")
            assignment = impl.prepare(repo, self.store, self.owner, impl.ImplementationPrepare(
                feature_id=found["feature_id"], key="i1", author_id="story-worker", project=PROJECT,
                client=CLAUDE))["assignment_id"]
            return repo, assignment, worker.run_assignment(repo, self.store, self.owner, assignment, "fake-model", 5)

    def reseal(self, assignment, terminal):
        """Rewrite return.json and re-anchor its digest in the DB, as a forger with DB access could."""
        data = canonical(terminal)
        (self.run_dir(assignment) / "return.json").write_bytes(data)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE events SET payload_digest=? WHERE subject_id=? AND kind IN "
                         "('assignment.return-sealed', 'assignment.return-observed')", (sha(data), assignment))
            stored = conn.execute("SELECT return_json FROM assignments WHERE id=?", (assignment,)).fetchone()[0]
            conn.execute("UPDATE assignments SET return_json=? WHERE id=?",
                         (json.dumps({**json.loads(stored), "payload_digest": sha(data)}), assignment))

    def codex_bundle(self):
        self.planning()
        author = self.prep("p1")["assignment_id"]
        report = self.execute(author, flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        return author, self.bundled()["id"]

    def sealed(self, assignment):
        binding = self.binding_of_review(assignment)
        return binding["sealed_root"], [item["sealed_path"] for item in binding["sealed_files"]]

    def review_run(self, bundle, key="r1", hashed=False, **env):
        assignment = self.prepared(bundle, key, client=CLAUDE)["assignment_id"]
        if hashed:
            env["FAKE_BASH"] = json.dumps([["sha256sum " + " ".join(self.sealed(assignment)[1]), False]])
        return assignment, self.run_it(assignment, **{"FAKE_EXTRA": json.dumps(self.final_for(assignment)), **env})

    # -- argv ------------------------------------------------------------------------------------------------------

    def test_author_argv_exact_flags_isolation_and_three_script_rules_plus_stdin_sha256sum(self):
        commands = ("uv run --no-project /w/_bmad/scripts/resolve_customization.py --skill /w/.agents/skills/bmad-prd",
                    "uv run --no-project /w/_bmad/scripts/resolve_config.py --project-root /w",
                    "uv run --no-project /w/_bmad/scripts/memlog.py init --workspace ws",
                    "uv run --no-project /w/_bmad/scripts/memlog.py append --workspace ws")
        self.assertEqual(["/bin/claude", "-p", "--output-format", "stream-json", "--verbose", "--session-id", SESSION,
                          "--no-session-persistence", "--model", "sonnet", "--max-turns", "60",
                          "--json-schema", '{"type":"object"}', "--setting-sources", "", "--strict-mcp-config",
                          "--mcp-config", '{"mcpServers":{}}', "--disallowedTools", "mcp__*", *DENIED,
                          "--tools", "Read,Grep,Glob,Edit,Write,Bash", "--permission-mode", "acceptEdits",
                          "--permission-prompts", "none", "--allowedTools",
                          *[f"Bash({item} *)" for item in commands], "Bash(sha256sum)"],
                         clients.CLAUDE.argv(self.launch_spec(commands=commands)))
        self.assertEqual(24, len(set(DENIED)))
        for spec in (self.launch_spec(commands=commands), self.launch_spec("read-only", read_dirs=("/s",)),
                     self.launch_spec("read-only", isolated=True)):
            self.assertEqual(1, clients.CLAUDE.argv(spec).count("--disallowedTools"))  # the flag never repeats
        self.assertEqual(["--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                          "--disallowedTools", "mcp__*"], clients.ISOLATION)
        self.planning()
        prepared = self.prep("p1", client=CLAUDE)
        binding = self.binding_of(prepared["assignment_id"])
        w = binding["worktree"]
        expected = [f"uv run --no-project {w}/_bmad/scripts/resolve_customization.py --skill {w}/.agents/skills/bmad-prd",
                    f"uv run --no-project {w}/_bmad/scripts/resolve_config.py --project-root {w}",
                    f"uv run --no-project {w}/_bmad/scripts/memlog.py init --workspace {WS}",
                    f"uv run --no-project {w}/_bmad/scripts/memlog.py append --workspace {WS}"]
        self.assertEqual((CLAUDE, expected), (binding["client"], binding["commands"]))
        report = self.execute(prepared["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        argv = self.argv(report)
        self.assertEqual([*[f"Bash({item} *)" for item in expected], "Bash(sha256sum)"],
                         argv[argv.index("--allowedTools") + 1:])
        self.assertEqual((Path(report.run_dir) / "return.schema.json").read_text(), argv[argv.index("--json-schema") + 1])
        self.assertEqual(str(self.fake_dir / "claude"), argv[0])
        self.assertFalse(set(argv) & set(FORBIDDEN[CLAUDE]))

    def test_generic_author_gets_no_bash_rules(self):
        self.assertEqual([], clients.author_rules(self.launch_spec()))
        self.assertEqual([*DENIED, "--tools", "Read,Grep,Glob,Edit,Write,Bash", "--permission-mode", "acceptEdits",
                          "--permission-prompts", "none"], clients.CLAUDE.argv(self.launch_spec())[21:])
        _, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        argv = self.argv(report)
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual([], [item for item in argv if "Bash(" in item])
        self.assertEqual("Read,Grep,Glob,Edit,Write,Bash", argv[argv.index("--tools") + 1])
        self.assertEqual(["mcp__*", *DENIED], argv[argv.index("--disallowedTools") + 1:argv.index("--tools")])

    def test_web_worker_argv_adds_the_two_tools_and_rules_and_the_facts_say_web(self):
        """Design D23: `web` widens --tools and appends two allow rules after the Bash rules; nothing else moves."""
        commands = ("python3",)
        plain = clients.CLAUDE.argv(self.launch_spec(commands=commands))
        web = clients.CLAUDE.argv(replace(self.launch_spec(commands=commands), web=True))
        self.assertEqual("Read,Grep,Glob,Edit,Write,Bash,WebFetch,WebSearch", web[web.index("--tools") + 1])
        self.assertEqual(["Bash(python3 *)", "Bash(sha256sum)", "WebFetch", "WebSearch"],
                         web[web.index("--allowedTools") + 1:])
        self.assertEqual(plain[:plain.index("--tools")], web[:web.index("--tools")])
        self.assertEqual(["Bash(python3 *)", "Bash(sha256sum)"], plain[plain.index("--allowedTools") + 1:])
        self.assertNotIn("web", clients.CLAUDE.launch_facts(self.launch_spec(commands=commands)))
        self.assertIs(True, clients.CLAUDE.launch_facts(replace(self.launch_spec(commands=commands), web=True))["web"])

    def test_review_and_isolated_launches_never_get_web(self):
        for spec in (self.launch_spec("read-only", read_dirs=("/sealed",)), self.launch_spec("read-only", isolated=True)):
            with self.subTest(isolated=spec.isolated):
                argv = clients.CLAUDE.argv(replace(spec, web=True))
                self.assertEqual(clients.CLAUDE.argv(spec), argv)
                self.assertNotIn("WebFetch", " ".join(argv))
                self.assertNotIn("web", clients.CLAUDE.launch_facts(replace(spec, web=True)))

    def test_launch_spec_web_defaults_to_false(self):
        self.assertIs(False, self.launch_spec().web)

    def test_tool_check_allows_the_web_tools_only_for_a_web_writer(self):
        """Design D23, ruling P2: the allowed set gains the two web tools only from sealed `web`, never read-only."""
        facts = {"tools": AUTHOR + ["WebFetch", "WebSearch", "StructuredOutput"]}
        self.code("UNKNOWN_EVIDENCE", clients.tool_check, clients.CLAUDE, "workspace-write", facts)
        self.assertEqual("passed", clients.tool_check(clients.CLAUDE, "workspace-write", facts, web=True))
        reader = {"tools": ["Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch", "StructuredOutput"]}
        self.code("UNKNOWN_EVIDENCE", clients.tool_check, clients.CLAUDE, "read-only", reader, web=True)
        self.assertEqual("not-checked", clients.tool_check(clients.CODEX, "workspace-write", facts, web=True))

    def test_reviewer_argv_read_tools_sha256sum_rule_only_dontask_and_sealed_add_dir(self):
        spec = self.launch_spec("read-only", read_dirs=("/sealed",), commands=("uv run x",))
        self.assertEqual(["--tools", "Read,Grep,Glob,Bash", "--permission-mode", "dontAsk", "--allowedTools",
                          "Read,Grep,Glob,Bash(sha256sum *)", "--add-dir", "/sealed"], clients.CLAUDE.argv(spec)[21:])
        reader = self.assign(idempotency_key="reader", permission_mode="read-only", writable_paths=[])
        report = self.go(reader)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        argv = self.argv(report)
        self.assertEqual(["--tools", "Read,Grep,Glob,Bash", "--permission-mode", "dontAsk", "--allowedTools",
                          "Read,Grep,Glob,Bash(sha256sum *)"], argv[21:])
        _, bundle = self.codex_bundle()
        reviewer, report = self.review_run(bundle)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        argv = self.argv(report)
        self.assertEqual([self.sealed(reviewer)[0]], [argv[index + 1] for index, item in enumerate(argv)
                                                     if item == "--add-dir"])
        self.assertNotIn("--permission-prompts", argv)

    def test_isolated_argv_has_no_bash_and_no_add_dir(self):
        argv = clients.CLAUDE.argv(self.launch_spec("read-only", isolated=True, read_dirs=("/sealed",),
                                                    commands=("uv run x",)))
        self.assertEqual(["--tools", "Read,Grep,Glob", "--permission-mode", "dontAsk", "--allowedTools",
                          "Read,Grep,Glob"], argv[21:])
        self.assertEqual([], [item for item in argv if "Bash" in item or item == "--add-dir"])
        run = self.isolated("one")
        self.assertEqual("observed", run.state, run.needs)
        directory = Path(run.receipt_dir)
        launch = json.loads((directory / "launch.json").read_bytes())
        self.assertEqual(argv[21:], launch["argv"][21:])
        facts = json.loads((directory / "return.json").read_bytes())["client_facts"]
        self.assertEqual((["Read", "Grep", "Glob"], None, "passed"),
                         (facts["tools"], facts["bundle_read_basis"], facts["tool_check"]))
        bash = self.isolated("bash", FAKE_TOOLS=json.dumps(["Read", "Grep", "Glob", "Bash"]))
        self.assertEqual(("unknown", None), (bash.state, bash.return_sha256))

    # -- launch receipt and session ---------------------------------------------------------------------------------

    def test_launch_receipt_records_isolation_env_inherited_names_session_and_max_turns(self):
        secrets = {"ANTHROPIC_API_KEY": "sk-ant-fake-secret-value", "CLAUDE_CONFIG_DIR": "/private/fake-config-dir",
                   "CLAUDE_CODE_MESSAGING_TOKEN": "parent-messaging-token-value"}
        assignment = self.assign()
        report = self.go(assignment, max_turns=7, FAKE_WRITE=writes(("result.md", "r")), **secrets)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        launch = self.receipt(report, "launch.json")
        with patch.dict(os.environ, {**self.env, **secrets}):
            removed = sorted(name for name in os.environ if name in clients.CLAUDE.env_unset or
                             (name.startswith("CLAUDE") and name not in clients.KEEP_ENV))
            kept = sorted(name for name in os.environ if name.startswith("CLAUDE") and name in clients.KEEP_ENV)
            inherited = sorted(name for name in os.environ
                               if name.startswith(("CLAUDE", "ANTHROPIC")) and name not in removed)
        self.assertEqual(["ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"], [name for name in inherited if name in secrets])
        self.assertIn("CLAUDE_CODE_MESSAGING_TOKEN", removed)
        self.assertIn("CLAUDE_CONFIG_DIR", kept)
        self.assertEqual({"flags": clients.ISOLATION, "inherited_names": inherited, "removed_names": removed,
                          "kept_names": kept, "env": CLIENT_ENV}, launch["isolation"])
        self.assertEqual(("workspace-write", "tool-policy", 7, CLAUDE),
                         (launch["requested_mode"], launch["confinement"], launch["max_turns"], launch["client"]))
        self.assertNotIn("requested_sandbox", launch)
        argv = launch["argv"]
        self.assertEqual((launch["session_id"], "7"),
                         (argv[argv.index("--session-id") + 1], argv[argv.index("--max-turns") + 1]))
        self.assertEqual((sorted(CLIENT_ENV), ["AI_AGENT", "CLAUDE*", "COREPACK_ENABLE_AUTO_PIN", "GIT_EDITOR",
                                               "NoDefaultCurrentDirectoryInExePath"]),
                         (launch["env_names"], launch["env_unset"]))
        for path in Path(report.run_dir).iterdir():
            if path.is_file():
                for value in secrets.values():
                    self.assertNotIn(value.encode(), path.read_bytes(), path.name)
        default = clients.CLAUDE.launch_facts(self.launch_spec())
        self.assertEqual((60, SESSION, "workspace-write", "tool-policy"),
                         (default["max_turns"], default["session_id"], default["requested_mode"],
                          default["confinement"]))

    def test_session_preset_is_fresh_uuid4_and_echo_mismatch_is_unknown(self):
        sessions = []
        for key in ("one", "two"):
            _, report = self.author(key=key)
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            session = self.receipt(report, "launch.json")["session_id"]
            self.assertEqual((4, session), (uuid.UUID(session).version, str(uuid.UUID(session))))
            self.assertEqual(session, self.receipt(report, "launch-intent.json")["session_id"])
            terminal = self.receipt(report)
            self.assertEqual((session, f"{CLAUDE}:{session}"), (terminal["thread_id"], terminal["source_id"]))
            seen = clients.jsonl_records(Path(report.run_dir) / "events.jsonl")
            self.assertEqual({session}, {item["session_id"] for item in seen if "session_id" in item})
            sessions.append(session)
        self.assertNotEqual(*sessions)
        self.failing("echo", FAKE_SESSION=str(uuid.uuid4()))
        self.failing("reuse", FAKE_SESSION=sessions[0])
        launch = self.direct()
        self.assertEqual(SESSION, self.terminal(launch, self.records(launch)).thread_id)
        other = str(uuid.uuid4())
        for index in (0, 1, -1):
            with self.subTest(index=index):
                records = self.records(launch, bash=[["sha256sum /x", False]])
                records[index] = {**records[index], "session_id": other}
                self.unknown(launch, records)
        self.unknown({key: value for key, value in launch.items() if key != "session_id"}, self.records(launch))
        self.unknown(launch, self.records(launch, init={"session_id": ABSENT}))

    # -- terminal rule ---------------------------------------------------------------------------------------------

    def test_init_first_and_single_result_last_or_unknown(self):
        self.failing("no-init", FAKE_MODE="no-init")
        self.failing("truncated", FAKE_MODE="truncated")
        launch = self.direct()
        init, result = self.records(launch)
        tool = {"type": "assistant", "session_id": SESSION, "message": {"content": []}}
        for case in ([tool, init, result], [init, init, result], [init, result, result], [init, result, tool],
                     [result], [init], [init, b"not json", result], [init, {"subtype": "x"}, result],
                     [init, b"", result], [init, b"[]", result]):
            with self.subTest(case=case):
                self.unknown(launch, case)
        self.assertEqual(canonical({"ok": True}), self.terminal(launch, [init, tool, result]).final)

    def test_structured_output_written_canonically_to_last_json_and_replay_recomputes_it(self):
        assignment, report = self.author()
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        directory = Path(report.run_dir)
        raw = (directory / "events.jsonl").read_bytes()
        structured = clients.jsonl_records(directory / "events.jsonl")[-1]["structured_output"]
        last = (directory / "last.json").read_bytes()
        self.assertEqual(canonical(structured), last)
        self.assertIn(b'"assignment_id": ', raw)  # the stream is not canonical; last.json is runner-written
        self.assertNotIn(last, raw)
        self.assertEqual(0o600, (directory / "last.json").stat().st_mode & 0o777)
        self.assertEqual(sha(last), self.receipt(report)["last_sha256"])
        self.assertEqual(last, clients.CLAUDE.terminal(directory, self.receipt(report, "launch.json")).final)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
        self.assertEqual(self.receipt(report)["source_id"],
                         historical_return(self.t.repo, self.store, assignment).source_id)

    def test_error_subtypes_is_error_or_missing_structured_output_are_unknown(self):
        cases = [{"FAKE_SUBTYPE": subtype} for subtype in
                 ("error_max_turns", "error_during_execution", "error_max_structured_output_retries")]
        cases += [{"FAKE_MODE": "is-error"}, {"FAKE_MODE": "no-structured"}]
        for index, env in enumerate(cases):
            with self.subTest(env=env):
                report = self.failing(f"error-{index}", FAKE_WRITE=writes(("result.md", "e")), **env)
                self.assertEqual(0, self.receipt(report, "end.json")["exit_code"])  # the stream decides, not exit
                self.assertFalse((Path(report.run_dir) / "last.json").exists())
        launch = self.direct()
        for result in ({"is_error": "false"}, {"is_error": None}, {"structured_output": ["not", "object"]},
                       {"structured_output": None}, {"subtype": None}):
            with self.subTest(result=result):
                self.unknown(launch, self.records(launch, result=result))

    def test_auth_failure_is_unknown_and_keeps_fence(self):
        t = self.tree("auth")
        assignment = self.assign(t, idempotency_key="auth")
        report = self.go(assignment, t, FAKE_MODE="auth-fail")
        self.held(report, assignment)
        self.assertEqual(1, self.receipt(report, "end.json")["exit_code"])
        self.assertEqual(["Worker process exited unsuccessfully"], report.evidence_needs)   # design D25: no stop
        self.assertFalse((Path(report.run_dir) / "last.json").exists())
        # the retained stream alone fails closed too, whatever the exit code
        self.code("UNKNOWN_EVIDENCE", clients.CLAUDE.terminal, Path(report.run_dir), self.receipt(report, "launch.json"))
        other = self.assign(t, idempotency_key="after-auth")
        self.code("BLOCKED_EVIDENCE", self.coordinator.reserve_launch, self.owner, other, CLAUDE)

    def test_permission_denials_recorded_as_client_facts_without_changing_validity(self):
        denials = [{"tool_name": "Bash", "tool_use_id": "toolu_9", "tool_input": {"command": "git status"}}]
        assignment, report = self.author(FAKE_DENIALS=json.dumps(denials))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual("observed", self.coordinator.detail("assignment", assignment)["return"]["provenance_class"])
        launch = self.receipt(report, "launch.json")
        self.assertEqual({"permission_denials": denials, "num_turns": 3, "terminal_reason": "completed",
                          "stop_reason": "end_turn", "models": ["claude-fake-1"], "total_cost_usd": 0.0123,
                          "init": {"tools": AUTHOR, "mcp_servers": [], "permissionMode": "acceptEdits",
                                   "cwd": launch["worktree"], "model": "claude-fake-1"},
                          "isolation_gaps": [], "bundle_read_basis": None, "tools": AUTHOR, "tool_check": "passed"},
                         self.facts(report))
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_init_tool_outside_allowlist_is_unknown_including_unnamed_tools(self):
        self.failing("todo", FAKE_TOOLS=json.dumps(AUTHOR + ["TodoWrite"]))
        self.failing("unnamed", FAKE_TOOLS=json.dumps(AUTHOR + [""]))
        launch = self.direct()
        for tools in (["Read", "WebFetch"], ["Read", ""], ["Read", None], ["Read", {"name": "Read"}], "Read",
                      ["Task"], {"Read": True}):
            with self.subTest(tools=tools):
                self.unknown(launch, self.records(launch, init={"tools": tools}))
        reader = self.direct("read-only")
        self.unknown(reader, self.records(reader, init={"tools": ["Read", "Edit"]}))
        self.assertEqual(["Read", "Bash"], self.terminal(launch, self.records(launch, init={"tools": ["Read", "Bash"]}))
                         .facts["tools"])
        self.assertEqual(frozenset({"StructuredOutput"}), clients.CLAUDE.internal_tools)  # P0, --json-schema
        facts = self.terminal(launch, self.records(launch, init={"tools": AUTHOR + ["StructuredOutput"]})).facts
        self.assertEqual("passed", clients.tool_check(clients.CLAUDE, "workspace-write", facts))
        with patch.object(clients, "CLAUDE", replace(clients.CLAUDE, internal_tools=frozenset({"TodoWrite"}))):
            facts = self.terminal(launch, self.records(launch, init={"tools": ["Read", "TodoWrite"]})).facts
            self.assertEqual("passed", clients.tool_check(clients.CLAUDE, "workspace-write", facts))
        self.assertEqual(frozenset(AUTHOR + ["StructuredOutput"]), clients.CLAUDE.allowed_tools("workspace-write"))
        self.assertEqual(frozenset({"Read", "Grep", "Glob", "Bash", "StructuredOutput"}),
                         clients.CLAUDE.allowed_tools("read-only"))

    def test_mcp_tools_recorded_as_isolation_gaps(self):
        servers = [{"name": "claude.ai Gmail", "status": "connected"}]
        assignment, report = self.author(FAKE_TOOLS=json.dumps(AUTHOR + ["mcp__claude_ai_Gmail__send"]),
                                         FAKE_MCP=json.dumps(servers))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        facts = self.facts(report)
        self.assertEqual((["mcp-server:claude.ai Gmail", "mcp-tool:mcp__claude_ai_Gmail__send"], AUTHOR, "passed",
                          servers), (facts["isolation_gaps"], facts["tools"], facts["tool_check"],
                                     facts["init"]["mcp_servers"]))
        self.assertEqual(AUTHOR + ["mcp__claude_ai_Gmail__send"], facts["init"]["tools"])
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        launch = self.direct()
        for value, gaps in ((ABSENT, []), ([], []), ([{"status": "x"}], ['mcp-server:{"status":"x"}'])):
            with self.subTest(value=value):
                facts = self.terminal(launch, self.records(launch, init={"mcp_servers": value})).facts
                self.assertEqual(gaps, facts["isolation_gaps"])
        self.unknown(launch, self.records(launch, init={"mcp_servers": {"gmail": {}}}))

    def test_init_cwd_or_mode_mismatch_is_unknown_and_absence_is_a_gap(self):
        launch = self.direct()
        self.unknown(launch, self.records(launch, init={"cwd": "/elsewhere"}))
        self.unknown(launch, self.records(launch, init={"permissionMode": "bypassPermissions"}))
        facts = self.terminal(launch, self.records(launch, init={"cwd": ABSENT, "permissionMode": ABSENT})).facts
        self.assertEqual(["init-cwd-absent", "init-permissionMode-absent"], facts["isolation_gaps"])
        self.assertEqual((None, None), (facts["init"]["cwd"], facts["init"]["permissionMode"]))
        isolated = self.direct("read-only", isolated=True)
        self.assertEqual([], self.terminal(isolated, self.records(isolated)).facts["isolation_gaps"])
        self.unknown(isolated, self.records(isolated, init={"permissionMode": "acceptEdits"}))
        self.unknown({**isolated, "cwd": "/other"}, self.records(isolated))

    # -- replay ----------------------------------------------------------------------------------------------------

    def test_edited_client_facts_or_last_json_makes_inspect_unknown(self):
        assignment, report = self.author(FAKE_DENIALS=json.dumps([{"tool_name": "Write"}]))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        terminal = self.receipt(report)
        facts = terminal["client_facts"]
        forged = [{**terminal, "client_facts": {**facts, "permission_denials": []}},
                  {**terminal, "client_facts": {**facts, "bundle_read_basis": "command"}},
                  {**terminal, "client_facts": {**facts, "isolation_gaps": []} | {"tool_check": "blocked"}},
                  {key: value for key, value in terminal.items() if key != "client_facts"}]
        for index, value in enumerate(forged):
            with self.subTest(index=index):
                self.reseal(assignment, value)
                checked = inspect_run(self.t.repo, self.store, assignment)
                self.assertEqual("unknown", checked.state)
                self.assertIn("Client facts", checked.evidence_needs[0])
        self.reseal(assignment, terminal)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        last = Path(report.run_dir) / "last.json"
        original = last.read_bytes()
        last.write_bytes(original.replace(b"}", b" }", 1))
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        last.write_bytes(original)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    def test_codex_replay_without_client_facts_stays_observed(self):
        assignment = self.assign()
        report = self.go(assignment, client=clients.DEFAULT, FAKE_WRITE=writes(("result.md", "codex")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        terminal = self.receipt(report)
        self.assertNotIn("client_facts", terminal)
        self.assertEqual("codex-cli", terminal["client"])
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)
        self.assertEqual("completed-claim", reconcile_run(self.t.repo, self.store, self.owner, assignment).state)
        historical_return(self.t.repo, self.store, assignment)
        self.reseal(assignment, {**terminal, "client_facts": {"tool_check": "passed"}})
        self.assertEqual("unknown", inspect_run(self.t.repo, self.store, assignment).state)
        self.reseal(assignment, terminal)
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, assignment).state)

    # -- reviewer Bash audit ---------------------------------------------------------------------------------------

    def test_reviewer_bash_audit_bare_sealed_sha256sum_is_command_else_claim(self):
        _, bundle = self.codex_bundle()
        reviewer, report = self.review_run(bundle, "r1", hashed=True)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual("command", self.facts(report)["bundle_read_basis"])
        self.assertEqual("observed", self.record(reviewer)["provenance_class"])
        launch = self.receipt(report, "launch.json")
        root, files = self.sealed(reviewer)
        self.assertGreaterEqual(len(files), 2)
        whole = "sha256sum " + " ".join(files)

        def basis(*bash):
            return self.terminal(launch, self.records(launch, bash=bash)).facts["bundle_read_basis"]

        self.assertEqual("command", basis([whole, False]))
        self.assertEqual("command", basis(*[["sha256sum " + item, False] for item in files]))
        self.assertEqual("command", basis([whole + f" {root}/manifest.json", False]))
        claims = {"pipe": whole + " | cat", "chain": whole + "; true", "and": whole + " && true",
                  "redirect": whole + " > out.txt", "subshell": f"sha256sum $(ls {root}) " + " ".join(files),
                  "env": "LC_ALL=C " + whole, "outside": whole + " /etc/hostname", "dotdot": whole + f" {root}/../x",
                  "relative": whole + " artifact/prd.md", "tab": whole.replace(" ", "\t", 1),
                  "double-space": whole.replace(" ", "  ", 1), "quoted": whole + f" '{root}/x'",
                  "bare": "sha256sum", "glob": whole + f" {root}/*"}
        for name, command in claims.items():
            with self.subTest(name=name):
                self.assertEqual("claim", basis([command, False]))
        self.assertEqual("claim", basis(["sha256sum " + files[0], False]))  # an unhashed sealed file
        self.assertEqual("claim", basis([whole, True]))  # an error result is not a hash
        self.assertEqual("claim", basis([whole, False], ["git status", True]))  # a denied non-sha256sum command
        self.assertEqual("claim", basis())  # no Bash stream at all
        self.assertIsNone(self.terminal(self.direct("read-only"), self.records(self.direct("read-only")))
                          .facts["bundle_read_basis"])  # unbound read-only
        self.assertIsNone(self.terminal(self.direct(), self.records(self.direct(), bash=[[whole, False]]))
                          .facts["bundle_read_basis"])  # author
        # downgrade only: a claim basis never changes validity or the observed review
        second, claimed = self.review_run(bundle, "r2", FAKE_BASH=json.dumps([[whole + " | cat", False]]))
        self.assertEqual("completed-claim", claimed.state, claimed.evidence_needs)
        self.assertEqual("claim", self.facts(claimed)["bundle_read_basis"])
        self.assertEqual("observed", self.record(second)["provenance_class"])

    # -- bound flows -------------------------------------------------------------------------------------------------

    def test_claude_bound_planning_create_question_decision_update_end_to_end(self):
        self.planning()
        questions = ("Which cloud?", "Which region?")
        first = self.prep("p1", client=CLAUDE)["assignment_id"]
        report = self.execute(first, flow("partial", open_questions=list(questions)),
                              self.first_files(extra=[(f"{WS}/notes.md", "partial\n")]), "needs-decision",
                              question_text(questions))
        self.assertEqual("needs-decision", report.state, report.evidence_needs)
        request = self.request_of(first)
        self.decide_request(request)
        update = self.prep("u1", client=CLAUDE, **self.update_args(first, request))["assignment_id"]
        final = {"result": "completed", "workflow_json": json.dumps(flow(intent="update")),
                 "unresolved_question": None, "customization": self.custom()}
        with patch.dict(os.environ, {"FAKE_WRITE": writes((PRD_FILE, "# PRD v2\n"), (MEMLOG, "log\nchange\n")),
                                     "FAKE_EXTRA": json.dumps(final)}):
            self.assert_fakes()
            result = json.loads(self.cli("bmad", "run", update, "--token-file", str(self.token), "--model", "sonnet",
                                         "--timeout", "5", "--client", CLAUDE, "--max-turns", "9").stdout)
        self.assertEqual(("completed-claim", "OBSERVED_COMPLETE_CLAIM"), (result["state"], result["verdict"]))
        for assignment in (first, update):
            with self.subTest(assignment=assignment):
                stored = self.detail(assignment)["return"]
                self.assertEqual((CLAUDE, "observed"), (clients.client_of(stored["source_id"]),
                                                        stored["provenance_class"]))
                self.assertEqual([CLAUDE], self.reserved(assignment))
                binding = self.binding_of(assignment)
                launch = json.loads((self.run_dir(assignment) / "launch.json").read_bytes())
                self.assertEqual((CLAUDE, CLAUDE), (binding["client"], launch["client"]))
                self.assertEqual([*[f"Bash({item} *)" for item in binding["commands"]], "Bash(sha256sum)"],
                                 launch["argv"][launch["argv"].index("--allowedTools") + 1:])
        self.assertEqual(9, json.loads((self.run_dir(update) / "launch.json").read_bytes())["max_turns"])
        self.assertEqual("completed-claim", inspect_run(self.t.repo, self.store, update).state)
        self.assertEqual("OBSERVED_COMPLETE_CLAIM", planning.view_return(self.t.repo, self.store, update)["verdict"])

    def test_claude_reviewer_of_codex_authors_is_observed_and_reused_session_refused(self):
        author, bundle = self.codex_bundle()
        reviewer, report = self.review_run(bundle, "r1")
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        recorded = self.record(reviewer)
        self.assertEqual("observed", recorded["provenance_class"])
        sources = [self.detail(item)["return"]["source_id"] for item in (author, reviewer)]
        self.assertEqual(["codex-cli", CLAUDE], [clients.client_of(item) for item in sources])
        self.assertEqual("claim", self.facts(report)["bundle_read_basis"])  # no sha256sum run: a model claim
        session = self.receipt(report, "launch.json")["session_id"]
        second = self.prepared(bundle, "r2", client=CLAUDE)["assignment_id"]
        report = self.run_it(second, FAKE_SESSION=session, FAKE_EXTRA=json.dumps(self.final_for(second)))
        self.held(report, second)
        self.assertNotEqual(session, self.receipt(report, "launch.json")["session_id"])
        self.code("BLOCKED_EVIDENCE", self.record, second)
        self.assertEqual([recorded["id"]], [row["id"] for row in self.store.read_snapshot()["reviews"]])

    # -- briefs, env and doctor --------------------------------------------------------------------------------------

    def test_claude_briefs_drop_bytecode_rule_and_name_exact_commands_while_codex_brief_bytes_match_golden(self):
        self.planning()
        codex = self.prep("p1")
        literals = {str(self.root.resolve()): "<ROOT>", str(self.root): "<ROOT>"}
        self.assertEqual((GOLDEN / "planning" / "brief.md").read_bytes(),
                         golden_normalize((self.t.path / codex["brief_ref"]).read_bytes(), literals))
        self.assertNotIn("commands", self.binding_of(codex["assignment_id"]))
        claude = self.prep("p2", slug="claude", client=CLAUDE)
        text = self.brief_of(claude)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", text)
        commands = self.binding_of(claude["assignment_id"])["commands"]
        self.assertIn(f"uv run --no-project {self.t.repo.worktree}/_bmad/scripts/memlog.py append --workspace "
                      f"{paths('claude')[0]}", commands)
        for command in commands:
            self.assertIn(f"`{command} …`", text)
        self.assertIn("`sha256sum` on stdin only", text)
        self.assertIn("`uv` calls other than these are denied; use the skill's documented fallback.", text)
        self.assertIn("The claude-code client does not auto-discover the skill", text)
        report = self.execute(codex["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.bundled()["id"]
        codex_review = (self.t.path / self.prepared(bundle, "r-codex")["brief_ref"]).read_text()
        claude_review = (self.t.path / self.prepared(bundle, "r-claude", client=CLAUDE)["brief_ref"]).read_text()
        self.assertIn("- Set `PYTHONDONTWRITEBYTECODE=1` for every command. Do not start extra reviewers beyond what "
                      "the installed review skill itself requires.", codex_review)
        self.assertNotIn("sha256sum", codex_review)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", claude_review)
        self.assertIn("- Write nothing anywhere; your sandbox is read-only. Do not edit the live worktree or the sealed "
                      "copy. Do not call any service or follow any handoff.", codex_review)
        self.assertNotIn("sandbox", claude_review)
        self.assertNotIn("sandbox", text)
        self.assertIn("- Write nothing anywhere; your tool policy has no write tools and denies write-capable shell "
                      "commands; every shell command is recorded and audited. It is a recorded policy, not OS "
                      "confinement.", claude_review)
        self.assertIn("run exactly `sha256sum <sealed path>…`; no env prefix, pipe, redirect or chaining; `uv` is "
                      "denied, so use the skill's documented fallback (read `customize.toml`)", claude_review)
        self.assertIn("- Do not start extra reviewers beyond what the installed review skill itself requires.",
                      claude_review)
        self.assertIn("- Use only Read, Grep, Glob and bare `sha256sum <sealed path>…`. Any other shell command, even a "
                      "read-only one that the CLI allows, makes your `bundle_read` a claim and fails the read gate.",
                      claude_review)
        self.assertNotIn("bundle_read` a claim", codex_review)

    def test_profile_env_and_client_env_reach_claude_process(self):
        self.planning()
        names = ("PYTHONDONTWRITEBYTECODE", *CLIENT_ENV)
        out = self.root / "env.json"
        assignment = self.prep("p1", client=CLAUDE)["assignment_id"]
        t = self.tree("generic")
        generic = self.assign(t, idempotency_key="generic")
        final = {"result": "completed", "workflow_json": json.dumps(flow()), "unresolved_question": None,
                 "customization": self.custom()}
        with patch.dict(os.environ):
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)  # only the profile env may set it
            report = self.run_it(assignment, FAKE_WRITE=writes(*self.first_files()), FAKE_EXTRA=json.dumps(final),
                                 FAKE_ENV_OUT=str(out))
            self.assertEqual("completed-claim", report.state, report.evidence_needs)
            self.assertEqual(["1", "1", "1", "1"], [json.loads(out.read_text())["env"][name] for name in names])
            plain = self.go(generic, t, FAKE_WRITE=writes(("result.md", "g")), FAKE_ENV_OUT=str(out))
            self.assertEqual("completed-claim", plain.state, plain.evidence_needs)
            self.assertEqual([None, "1", "1", "1"], [json.loads(out.read_text())["env"][name] for name in names])
        launch = self.receipt(report, "launch.json")
        self.assertEqual(({"PYTHONDONTWRITEBYTECODE": "1"}, sorted(CLIENT_ENV)), (launch["env"], launch["env_names"]))
        self.assertNotIn("env", self.receipt(plain, "launch.json"))
        version_env = self.root / "version-env.json"
        with patch.dict(os.environ, {**self.env, "FAKE_VERSION_ENV_OUT": str(version_env)}):
            self.assert_fakes()
            self.assertEqual((str(self.fake_dir / "claude"), VERSION), clients.preflight(clients.CLAUDE))
        self.assertLessEqual(set(CLIENT_ENV), set(json.loads(version_env.read_text())))

    def test_doctor_reports_claude_preflight(self):
        def doctor(*args, code=0, **env):
            environ = {**os.environ, **self.env, **env}
            found = shutil.which("claude", path=environ["PATH"])
            if "PATH" not in env:  # the fake comes first; a real client is never reached
                self.assertEqual(self.fake_dir, Path(found).parent)
            result = subprocess.run([sys.executable, str(self.t.path / ".bmad-coordinator" / "run.py"), "--repo",
                                     str(self.t.path), "doctor", "--base", self.base, *args], capture_output=True,
                                    text=True, env=environ)
            self.assertEqual(code, result.returncode, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual({"name": CLAUDE, "binary": str(self.fake_dir / "claude"), "version": VERSION,
                          "ready": True, "reasons": []}, doctor("--client", CLAUDE)["client"])
        failing = doctor("--client", CLAUDE, FAKE_VERSION_FAIL="exit")
        self.assertEqual((False, False, None), (failing["client"]["ready"], failing["ready"],
                                                failing["client"]["version"]))
        self.assertIn(f"Worker client {CLAUDE} is not ready", failing["reasons"])
        empty = self.root / "empty-bin"
        empty.mkdir()
        self.assertIsNone(shutil.which("claude", path=safe_path(empty)))
        missing = doctor("--client", CLAUDE, PATH=safe_path(empty))
        self.assertEqual((None, False), (missing["client"]["binary"], missing["ready"]))
        self.assertEqual(2, self.version_count())
        self.assertFalse(self.count.exists())  # a preflight is never a model call or a spawn

    # -- fix round 1 ---------------------------------------------------------------------------------------------

    def test_modified_allowed_script_is_unknown_through_contract_digests(self):
        self.planning()
        assignment = self.prep("p1", client=CLAUDE)["assignment_id"]
        w = self.binding_of(assignment)["worktree"]
        named = sorted({item.split(f"{w}/", 1)[1].split()[0] for item in self.binding_of(assignment)["commands"]})
        self.assertEqual(["_bmad/scripts/memlog.py", "_bmad/scripts/resolve_config.py",
                          "_bmad/scripts/resolve_customization.py"], named)
        contracts = {item["path"] for item in self.contracts(assignment)}
        self.assertLessEqual(set(named), contracts)  # the contract digests already cover every allowed script
        final = {"result": "completed", "workflow_json": json.dumps(flow()), "unresolved_question": None,
                 "customization": self.custom()}
        report = self.run_it(assignment, FAKE_WRITE=writes(*self.first_files()), FAKE_EXTRA=json.dumps(final),
                             FAKE_OUTSIDE_WRITE=json.dumps([f"{w}/_bmad/scripts/memlog.py"]))
        self.held(report, assignment)
        self.assertIn("Immutable source or contract bytes changed", report.evidence_needs[0])
        argv = self.argv(report)
        self.assertEqual(["mcp__*", *DENIED], argv[argv.index("--disallowedTools") + 1:argv.index("--tools")])

    def test_pattern_metacharacters_in_rule_paths_are_refused_before_any_write(self):
        self.planning()
        before = len(self.store.read_snapshot()["assignments"])
        for root in ("skills*", "a?b", "a[b]", "a{b}", "a,b", "a(b)", "a\tb"):
            with self.subTest(root=root):
                self.code("INVALID_INPUT", self.prep, "p-bad", slug="bad", client=CLAUDE, client_root=root)
        self.assertEqual(before, len(self.store.read_snapshot()["assignments"]))
        self.assertFalse((self.t.path / "_bmad-output/coordinator/briefs/p-bad.md").exists())
        good = ("/w", ".agents/skills", "bmad-prd", "ws/prd")
        self.assertEqual(4, len(planning.author_commands(*good)))
        for index, bad in ((0, "/w,x"), (0, "/w*"), (1, ".agents/{a}"), (3, "ws/[p]"), (3, "ws/p?")):
            with self.subTest(bad=bad):
                args = list(good)
                args[index] = bad
                self.code("INVALID_INPUT", planning.author_commands, *args)

    def test_malformed_bound_commands_refused_before_reservation(self):
        self.use()
        versions = self.version_count()
        cases = {"string": "uv run x", "dict": {"uv": "run"}, "number": [3], "empty": [""], "glob": ["uv run x*"],
                 "long": ["x" * 4097], "many": ["uv run x"] * 9}
        for name, value in cases.items():
            with self.subTest(name=name):
                t = self.tree("commands-" + name)
                assignment = self.assign(t, PLAN, idempotency_key=name)
                self.bind(assignment, t, client=CLAUDE, commands=value)
                self.code("INVALID_INPUT", self.run_it, assignment, t)
                self.assertEqual("prepared", self.state(assignment))
                self.assertEqual([], self.reserved(assignment))
                self.assertFalse(os.path.lexists(self.run_dir(assignment, t)))
        self.assertEqual(versions, self.version_count())
        self.assertEqual(("uv run x",), clients.bound_commands({"commands": ["uv run x"]}))
        self.assertEqual((), clients.bound_commands({}))
        self.assertEqual((), clients.bound_commands(None))

    def test_empty_init_tools_list_is_blocked_like_absent_tools(self):
        launch = self.direct()
        for value in ([], ["mcp__x__y"], ABSENT):
            with self.subTest(value=value):
                facts = self.terminal(launch, self.records(launch, init={"tools": value})).facts
                self.assertNotIn("tools", facts)
                self.assertEqual("blocked", clients.tool_check(clients.CLAUDE, "workspace-write", facts))
        for key, value, reported in (("empty", "[]", []), ("absent", "null", None)):
            with self.subTest(key=key):
                _, report = self.author(self.tree("tools-" + key), key, FAKE_TOOLS=value)
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                self.assertEqual(("blocked", reported), (self.facts(report)["tool_check"],
                                                         self.facts(report)["init"]["tools"]))

    def test_option_like_model_refused_before_reservation(self):
        assignment = self.assign()
        for model in ("--tools", "-p"):
            with self.subTest(model=model), patch.dict(os.environ, self.env):
                self.assert_fakes()
                self.code("INVALID_INPUT", worker.run_assignment, self.t.repo, self.store, self.owner, assignment,
                          model, 5, CLAUDE)
        self.assertEqual("prepared", self.state(assignment))
        self.assertEqual([], self.reserved(assignment))
        self.assertEqual(0, self.version_count())
        root = self.root / "isolated"
        root.mkdir()
        cwd = root / "model-cwd"
        cwd.mkdir()
        self.code("INVALID_INPUT", clients.run_isolated, CLAUDE, self.t.repo, root, cwd, root / "model-receipts",
                  {"role": "judge"}, worker._schema(), "--tools", 5)
        self.assertFalse((root / "model-receipts").exists())

    # -- post-smoke fix round --------------------------------------------------------------------------------------

    def test_parent_claude_session_variables_are_scrubbed_from_worker_and_preflight(self):
        parent = {"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "parent-token-value",
                  "CLAUDE_CODE_MESSAGING_SOCKET": "/run/parent.sock", "CLAUDE_CODE_SESSION_ID": "parent-session-id",
                  "CLAUDE_CONFIG_DIR": "/private/config", "ANTHROPIC_API_KEY": "sk-ant-kept-value",
                  "CLAUDE_CODE_OAUTH_TOKEN": "oauth-kept-value", "CLAUDE_CODE_USE_VERTEX": "1",
                  "AI_AGENT": "claude-code_parent", "GIT_EDITOR": "true",
                  "CLAUDE_CODE_SCRUB_SENTINEL": str(uuid.uuid4()), "SCRUB_DECOY": str(uuid.uuid4())}
        auth = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CONFIG_DIR"]
        self.assertLessEqual({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                              "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}, clients.KEEP_ENV)
        out = self.root / "env.json"
        _, report = self.author(FAKE_ENV_OUT=str(out), **parent)
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        seen = json.loads(out.read_text())["names"]
        with patch.dict(os.environ, {**self.env, **parent}):
            kept = clients.kept_env(clients.CLAUDE)  # whatever the runner's own env adds to the documented auth names
        self.assertLessEqual(set(auth), set(kept))
        expected = sorted([*kept, *[name for name in CLIENT_ENV if name.startswith("CLAUDE")]])
        self.assertEqual(expected, [name for name in seen if name.startswith("CLAUDE")])
        self.assertLessEqual({"ANTHROPIC_API_KEY", "SCRUB_DECOY"}, set(seen))  # unprefixed variables pass
        self.assertFalse({"AI_AGENT", "GIT_EDITOR", "CLAUDE_CODE_SCRUB_SENTINEL"} & set(seen))
        isolation = self.receipt(report, "launch.json")["isolation"]
        self.assertLessEqual({"CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_MESSAGING_SOCKET",
                              "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SCRUB_SENTINEL", "AI_AGENT", "GIT_EDITOR"},
                             set(isolation["removed_names"]))
        self.assertFalse(set(auth) & set(isolation["removed_names"]))
        self.assertLessEqual(set(auth), set(isolation["kept_names"]))  # kept and removed: names only
        for path in Path(report.run_dir).iterdir():
            if path.is_file():
                for value in ("parent-token-value", "/run/parent.sock", "parent-session-id", "oauth-kept-value",
                              parent["CLAUDE_CODE_SCRUB_SENTINEL"], "claude-code_parent"):
                    self.assertNotIn(value.encode(), path.read_bytes(), path.name)
        version_env = self.root / "version-env.json"
        with patch.dict(os.environ, {**self.env, **parent, "FAKE_VERSION_ENV_OUT": str(version_env)}):
            self.assert_fakes()
            clients.preflight(clients.CLAUDE)
            self.assertEqual(sorted(name for name in os.environ if name in clients.CLAUDE.env_unset or
                                    (name.startswith("CLAUDE") and name not in clients.KEEP_ENV)),
                             clients.removed_env(clients.CLAUDE))
        self.assertEqual(expected, [name for name in json.loads(version_env.read_text()) if name.startswith("CLAUDE")])
        t = self.tree("codex-env")  # ruling (ii): Codex drops the parent session's markers, recorded in a sidecar
        codex = self.go(self.assign(t, idempotency_key="codex"), t, client=clients.DEFAULT,
                        FAKE_WRITE=writes(("result.md", "c")), FAKE_ENV_OUT=str(out), **parent)
        self.assertEqual("completed-claim", codex.state, codex.evidence_needs)
        self.assertNotIn("CLAUDECODE", json.loads(out.read_text())["names"])
        self.assertIn("CLAUDECODE", self.receipt(codex, "env-scrub.json")["removed_names"])
        self.assertEqual(frozenset(), clients.CODEX.env_unset)  # launch.json bytes stay the golden shape

    def test_planning_briefs_of_both_clients_state_the_profile_return_rules(self):
        self.planning()
        briefs = {CLAUDE: self.brief_of(self.prep("p-claude", slug="claude", client=CLAUDE)),
                  clients.DEFAULT: self.brief_of(self.prep("p-codex", slug="codex"))}
        for name, text in briefs.items():
            with self.subTest(client=name):
                for rule in planning.RETURN_RULES:
                    self.assertIn(rule + "\n", text)
        rules = "\n".join(planning.RETURN_RULES)
        import re
        source = inspect.getsource(planning.check) + inspect.getsource(planning._normalize)
        refusals = [text.removeprefix("{label} ") for text in re.findall(r'_bad\(f?"([^"]+)"\)', source)]
        self.assertGreaterEqual(len(refusals), 11)
        for message in refusals:  # every refusal the planning check can produce is announced
            self.assertIn(message, rules)
        for message in ("generic result does not match the BMAD status", "complete status with open questions",
                        "question is not the canonical aggregate of the workflow questions",
                        "create must change both the artifact and the memlog",
                        "update changed neither memlog nor a pre-existing artifact",
                        "intent differs from the bound intent", "question lists are malformed or a handoff was claimed",
                        "path is missing or has an unresolved placeholder", "path is not the bound workspace path"):
            self.assertIn(message, rules)  # the brief quotes every refusal the profile check can give
            self.assertIn(message, inspect.getsource(planning.check) + inspect.getsource(planning._normalize))
        example = canonical({"conflicts": [], "open_questions": ["Which cloud?"]}).decode()
        self.assertIn(f"`{example}`", rules)

    # -- attempt-2 fix round ---------------------------------------------------------------------------------------

    def test_every_brief_variant_states_the_affected_ids_rule(self):
        from coordinator_core.service import Coordinator
        self.assertIn("Affected ID must be an assignment in this feature", inspect.getsource(Coordinator.record_return))
        self.assertIn("(refused: Affected ID must be an assignment in this feature)", planning.AFFECTED_IDS_RULE)
        self.assertIn(planning.AFFECTED_IDS_RULE, planning.RETURN_RULES)
        self.planning()
        codex = self.prep("p1")
        briefs = {"planning codex": self.brief_of(codex),
                  "planning claude": self.brief_of(self.prep("p2", slug="claude", client=CLAUDE))}
        report = self.execute(codex["assignment_id"], flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        bundle = self.bundled()["id"]
        for name, client in (("review codex", clients.DEFAULT), ("review claude", CLAUDE)):
            briefs[name] = (self.t.path / self.prepared(bundle, name.replace(" ", "-"), client=client)["brief_ref"]) \
                .read_text()
        for name, text in briefs.items():
            with self.subTest(brief=name):
                self.assertEqual(1, text.count(planning.AFFECTED_IDS_RULE + "\n"))

    # -- generic-instruction round ---------------------------------------------------------------------------------

    def test_generic_envelope_states_the_return_rules_and_the_affected_ids_rule(self):
        from coordinator_core.service import Coordinator
        sources = inspect.getsource(worker._final) + inspect.getsource(worker._return_evidence) + \
            inspect.getsource(Coordinator.record_return)
        import re
        generic = re.findall(r'DomainError\("UNKNOWN_EVIDENCE", "([^"]+)"\)',
                             inspect.getsource(worker._final) + inspect.getsource(worker._return_evidence))
        self.assertEqual(3, len(set(generic)))
        for message in [*generic, "Affected ID must be an assignment in this feature"]:
            self.assertIn(message, sources)  # the instructions quote every refusal the generic checks produce
            self.assertIn(message, worker.INSTRUCTIONS)
        for clause in ("Git does not ignore", "restored to its original bytes", "`_bmad/`"):
            self.assertIn(clause, worker.INSTRUCTIONS)
        self.assertTrue(worker.INSTRUCTIONS.endswith(worker.AFFECTED_IDS))
        self.assertEqual("- " + worker.AFFECTED_IDS, planning.AFFECTED_IDS_RULE)
        self.assertIn("decision", worker.AFFECTED_IDS.split("never ", 1)[1].split(";")[0])
        self.assertNotIn("deleted, each", worker.INSTRUCTIONS)  # deletions are refused by _return_evidence
        for protected in ("deleting, renaming or symlinking", "mode-only change", "`AGENTS.md`", "`CLAUDE.md`",
                          "`.bmad-coordinator/`", "`.agents/` or `.claude/` skill trees", "Git common directory",
                          "outside the granted scopes"):
            self.assertIn(protected, worker.INSTRUCTIONS)
        for client in (clients.DEFAULT, CLAUDE):
            with self.subTest(client=client):
                t = self.tree("generic-" + client)
                _, report = self.author(t, client, client=client)
                self.assertEqual("completed-claim", report.state, report.evidence_needs)
                envelope = json.loads((Path(report.run_dir) / "envelope.json").read_bytes())
                self.assertEqual(worker.INSTRUCTIONS, envelope["instructions"])

    # -- attempt-7 brief round -------------------------------------------------------------------------------------

    def test_ruled_client_briefs_require_verbatim_absolute_commands_and_rules_stay_absolute(self):
        self.planning()
        briefs = {client: self.brief_of(self.prep(f"p-{client}", slug=client, client=client))
                  for client in (CLAUDE, "copilot", clients.DEFAULT)}
        placeholder = {CLAUDE: planning.PLACEHOLDER_RULE, "copilot": planning.LAUNCHER_RULE}  # Copilot: ruling R3
        anchor = {CLAUDE: "- Run only these shell commands", "copilot": "Run only these shell commands"}
        for client in planning.RULED_CLIENTS:
            with self.subTest(client=client):
                text, rule = briefs[client], placeholder[client]
                self.assertEqual(1, text.count(rule + "\n"))
                self.assertLess(text.index(anchor[client]), text.index(rule))
        self.assertNotIn("{project-root}", briefs[clients.DEFAULT])  # Codex runs under an OS sandbox, no rules
        for phrase in ("`{project-root}` and `{skill-root}` placeholders", "copy the forms above verbatim",
                       "relative, differently spelled or re-ordered", "denied by policy; do not retry it"):
            for rule in placeholder.values():
                self.assertIn(phrase, rule)
        commands = planning.author_commands(Path("/w"), ".agents/skills", "bmad-prd", "ws/prd")
        self.assertEqual(4, len(commands))  # the rules themselves are unchanged: absolute prefixes only
        self.assertTrue(all(item.startswith("uv run --no-project /w/_bmad/scripts/") for item in commands))
        rules = clients.author_rules(self.launch_spec(commands=tuple(commands)))
        self.assertEqual([f"Bash({item} *)" for item in commands] + ["Bash(sha256sum)"], rules)

    def test_a_max_turns_result_is_a_terminal_with_a_stop_and_other_errors_stay_unknown(self):
        """Design D25: `error_max_turns` with `terminal_reason: max_turns` is a Terminal whose final is empty and whose
        stop names the turns; the same subtype without the reason, or any other error, is unknown."""
        launch = self.direct()
        init, result = self.records(launch)
        stopped = {**result, "subtype": "error_max_turns", "terminal_reason": "max_turns", "is_error": True, "num_turns": 61}
        stopped.pop("structured_output")
        terminal = self.terminal(launch, [init, stopped])
        self.assertEqual((b"", {"kind": "max-turns", "num_turns": 61}, 61), (terminal.final, terminal.stop, terminal.facts["num_turns"]))
        self.assertIsNone(self.terminal(launch, [init, result]).stop)
        for other in ({**stopped, "terminal_reason": "completed"}, {**stopped, "subtype": "error_during_execution"},
                      {**stopped, "subtype": "success"}):
            with self.subTest(other=other):
                self.unknown(launch, [init, other])

    def test_a_client_stop_of_a_generic_author_stays_unknown_with_the_cause(self):
        report = self.failing("stopped", FAKE_MODE="max-turns", FAKE_WRITE=writes(("result.md", "partial")))
        self.assertEqual(1, self.receipt(report, "end.json")["exit_code"])
        self.assertIn("turn limit", " ".join(report.evidence_needs))
        self.assertFalse((Path(report.run_dir) / "return.json").exists())

    def test_a_resealed_stop_receipt_with_forged_fields_is_unknown(self):
        """Design D25: the fields a stop fixes are re-derived from the retained stream, so a resealed `return.json`
        that forges one is refused as a forged final message is on the normal path."""
        repo, assignment, report = self.story_stop()
        self.assertEqual("client-stopped", report.state, report.evidence_needs)
        terminal = self.receipt(report)
        self.assertEqual("client-stopped", inspect_run(repo, self.store, assignment).state)
        for forged in ({**terminal, "unresolved_question": "forged", "exit_code": 0}, {**terminal, "checks": ["passed"]},
                       {**terminal, "claimed_paths": ["pkg/greet.py"]}, {**terminal, "affected_ids": [assignment]}):
            with self.subTest(forged=[key for key in forged if forged[key] != terminal[key]]):
                self.reseal(assignment, forged)
                checked = inspect_run(repo, self.store, assignment)
                self.assertEqual(("unknown", ["Stop receipt differs from the retained client output"]),
                                 (checked.state, checked.evidence_needs))
        self.reseal(assignment, terminal)
        self.assertEqual("client-stopped", inspect_run(repo, self.store, assignment).state)

    def test_an_isolated_run_stopped_at_its_turn_limit_is_unknown_with_the_cause(self):
        """Design D25: run_isolated reads the stream before the exit code; a stop saves nothing and names its cause."""
        run = self.isolated("stop", FAKE_MODE="max-turns")
        self.assertEqual("unknown", run.state)
        self.assertIn("turn limit", " ".join(run.needs))
