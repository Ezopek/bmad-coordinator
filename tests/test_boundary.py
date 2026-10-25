"""The launch boundary (launch-boundary design L2 to L8). Unit tests need nothing; the sandbox tests run a real
`bwrap` and skip where it is unusable, unless COORDINATOR_REQUIRE_BWRAP=1 (CI, L9) makes that a failure."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from coordinator_core import boundary, clients, worker
from coordinator_core.boundary import Mount
from coordinator_core.model import DomainError
from tests.test_bmad_core import CoreBase, writes

ROOT = Path(__file__).parents[1]


def _unusable() -> str | None:
    try:
        boundary.preflight(dict(os.environ))
    except DomainError as exc:
        return exc.message
    return None


REASON = _unusable()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class UnitTests(unittest.TestCase):
    def test_kinds(self):
        self.assertIsNone(boundary.check_kind(None))
        self.assertEqual("bwrap", boundary.check_kind("bwrap"))
        for bad in ("", "docker", "BWRAP", 1, ["bwrap"]):
            with self.assertRaises(DomainError) as caught:
                boundary.check_kind(bad)
            self.assertEqual("INVALID_INPUT", caught.exception.code)

    def test_order_parents_first_duplicates_dropped_conflicts_and_bad_paths_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep, mid = Path(tmp) / "a" / "b", Path(tmp) / "a"
            deep.mkdir(parents=True)
            mounts, absent = boundary.ordered([Mount("rw", str(deep)), Mount("tmpfs", str(mid)),
                                               Mount("rw", str(deep)), Mount("ro", f"{tmp}/missing")])
            self.assertEqual([Mount("tmpfs", str(mid)), Mount("rw", str(deep))], mounts)
            self.assertEqual([f"{tmp}/missing"], absent)
            for bad in ([Mount("ro", str(deep)), Mount("rw", str(deep))], [Mount("ro", "relative")],
                        [Mount("ro", f"{tmp}/a/../a")], [Mount("bind", str(deep))]):
                with self.assertRaises(DomainError) as caught:
                    boundary.ordered(bad)
                self.assertEqual("UNSUPPORTED_CLIENT_MODE", caught.exception.code)

    def test_wrap_keeps_the_group_and_puts_no_secret_in_argv(self):
        argv = boundary.wrap("/usr/bin/bwrap", [Mount("ro", "/usr"), Mount("symlink", "/bin", "usr/bin"),
                                                 Mount("tmpfs", "/h/u"), Mount("copy", "/h/u/.c.json"),
                                                 Mount("rw", "/h/u/.claude")], Path("/w"), ["claude", "-p"],
                             {"/h/u/.c.json": 7})
        self.assertNotIn("--new-session", argv)
        self.assertNotIn("--setenv", argv)
        self.assertEqual(["--die-with-parent", *boundary.NAMESPACES], argv[1:6])
        self.assertIn(["--perms", "0600", "--file", "7", "/h/u/.c.json"],
                      [argv[i:i + 5] for i in range(len(argv))])
        self.assertEqual(["--chdir", "/w", "--", "claude", "-p"], argv[-5:])

    def test_project_mounts_per_kind(self):
        run, cwd, tree, common = Path("/r/run"), Path("/s/cwd"), Path("/p/main"), Path("/p/main/.git")
        self.assertEqual([Mount("ro", "/s/cwd"), Mount("rw", "/r/run")], boundary.project_mounts("isolated", run, cwd))
        reviewer = boundary.project_mounts("reviewer", run, tree, tree, common, sealed=("/p/main/.git/coordinator/b/1",))
        self.assertEqual([Mount("ro", "/p/main"), Mount("ro", "/p/main/.git"), Mount("tmpfs", "/p/main/.git/coordinator"),
                          Mount("ro", "/p/main/.git/coordinator/b/1"), Mount("rw", "/r/run")], reviewer)
        writer = boundary.project_mounts("writer", run, tree, tree, common, others=("/p/main/wt/story", "/q/other"))
        self.assertIn(Mount("rw", "/p/main"), writer)
        self.assertIn(Mount("ro", "/p/main/.git"), writer)   # the Git entry over the read-write worktree
        self.assertIn(Mount("tmpfs", "/p/main/wt/story"), writer)   # a nested worktree hidden
        self.assertNotIn(Mount("tmpfs", "/q/other"), writer)   # one outside the tree is simply not mounted
        with self.assertRaises(ValueError):
            boundary.project_mounts("author", run, tree, tree, common)

    def test_an_alias_to_a_protected_path_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "repo/.git/coordinator").mkdir(parents=True)
            (base / "cache").symlink_to(base / "repo/.git/coordinator")
            (base / "fine").mkdir()
            protected = [base / "repo/.git", base / "repo/.git/coordinator"]
            boundary.check_outside([Mount("rw", str(base / "fine")), Mount("symlink", "/x", "/y")], protected)
            for mount in (Mount("rw", str(base / "cache")), Mount("ro", str(base)), Mount("ro", str(base / "repo/.git"))):
                with self.assertRaises(DomainError):
                    boundary.check_outside([mount], protected)

    def test_a_scripts_interpreter_and_its_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "py" / "bin").mkdir(parents=True)
            (base / "py" / "lib").mkdir()
            python = base / "py" / "bin" / "python3"
            python.write_text("")
            python.chmod(0o755)
            direct, via_env, native = base / "direct", base / "via-env", base / "native"
            direct.write_text(f"#!{python}\nprint(1)\n")
            via_env.write_text("#!/usr/bin/env -S python3 -u\n")
            native.write_bytes(b"\x7fELF")
            expected = [Mount("ro", str(base / "py" / "bin")), Mount("ro", str(base / "py" / "lib"))]
            self.assertEqual(expected, boundary.interpreter(direct, ""))
            self.assertEqual([*boundary.executable("/usr/bin/env"), *expected],
                             boundary.interpreter(via_env, str(base / "py" / "bin")))
            self.assertEqual([], boundary.interpreter(native, ""))
            self.assertEqual(boundary.executable("/usr/bin/env"), boundary.interpreter(via_env, str(base / "none")))

    def test_node_prefix_is_npm_global_layout_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            script = base / "node" / "lib" / "node_modules" / "@x" / "cli" / "bin" / "cli.js"
            script.parent.mkdir(parents=True)
            script.write_text("")
            (base / "node" / "bin").mkdir()
            (base / "node" / "bin" / "node").write_text("")
            self.assertEqual(base / "node", boundary.node_prefix(script))
            elsewhere = base / "tools" / "cli.js"
            elsewhere.parent.mkdir()
            elsewhere.write_text("")
            (base / "bin").mkdir()
            (base / "bin" / "node").write_text("")   # like a system /usr/bin/node above everything
            self.assertIsNone(boundary.node_prefix(elsewhere))

    def test_link_chain_and_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "real").mkdir()
            (base / "real/tool").write_text("#!/bin/sh\n")
            (base / "mid").symlink_to(base / "real/tool")
            (base / "bin").mkdir()
            (base / "bin/tool").symlink_to("../mid")
            self.assertEqual([Mount("symlink", str(base / "bin/tool"), "../mid"),
                              Mount("symlink", str(base / "mid"), str(base / "real/tool")),
                              Mount("ro", str(base / "real"))], boundary.executable(base / "bin/tool"))

    def test_record_and_its_replay_check(self):
        client = ["claude", "-p", "x"]
        wrapped = ["/usr/bin/bwrap", "--ro-bind", "/usr", "/usr", "--chdir", "/w", "--", *client]
        value = boundary.record("/usr/bin/bwrap", "bubblewrap 0.11.1", [Mount("ro", "/usr")], ["/gone"], {"B", "A"},
                                wrapped, token_source="gh")
        self.assertEqual({"kind": "bwrap", "binary": "/usr/bin/bwrap", "version": "bubblewrap 0.11.1",
                          "mounts": [{"mode": "ro", "path": "/usr"}], "absent": ["/gone"], "env_names": ["A", "B"],
                          "argv": wrapped, "token_source": "gh"}, value)
        self.assertEqual([], boundary.check_record(value, "bwrap", client))
        for broken in (None, {**value, "kind": "docker"}, {**value, "argv": "x"}, {**value, "argv": wrapped[:-1]},
                       {**value, "argv": [*wrapped[:-4], "--", "other", "-p", "x"][:-2] + client[1:]}):
            with self.subTest(broken=broken):
                self.assertTrue(boundary.check_record(broken, "bwrap", client))


class PipelineTests(unittest.TestCase):
    """Launch-boundary L1 and L4 in the pipeline, the prepares and the drive; no sandbox needed."""

    def template(self):
        return json.loads((ROOT / "examples" / "greetings" / "pipeline.json").read_text())

    def filled(self, template):
        from coordinator_core import drive_epic
        return drive_epic.fill_template(template, "0" * 32, "1-2-farewell")

    def test_the_role_key_and_the_project_mounts_are_validated(self):
        template = self.template()
        for role in template["roles"].values():
            role["boundary"] = "bwrap"
        template["project"]["boundary"] = {"ro": ["~/.cache/uv", "/opt/tool"], "rw": ["~/.npm"]}
        filled = self.filled(template)
        self.assertEqual("bwrap", filled["roles"]["worker"]["boundary"])
        for role, project in (({"boundary": "docker"}, None), ({"boundary": None}, None),
                              (None, {"ro": ["relative"]}), (None, {"ro": ["/a/../b"]}), (None, {"ro": ["/a*"]}),
                              (None, {"x": []}), (None, {"ro": "/a"})):
            with self.subTest(role=role, project=project):
                broken = self.template()
                if role is not None:
                    broken["roles"]["worker"].update(role)
                if project is not None:
                    broken["project"]["boundary"] = project
                with self.assertRaises(DomainError) as caught:
                    self.filled(broken)
                self.assertEqual("INVALID_INPUT", caught.exception.code)

    def test_a_binding_carries_the_boundary_only_when_set(self):
        from coordinator_core import bmad_planning
        from types import SimpleNamespace
        self.assertEqual({}, bmad_planning.client_binding(SimpleNamespace(client=clients.DEFAULT)))
        self.assertEqual({"boundary": "bwrap"}, bmad_planning.client_binding(
            SimpleNamespace(client=clients.DEFAULT, boundary="bwrap")))
        self.assertEqual({"client": "claude-code", "boundary": "bwrap"}, bmad_planning.client_binding(
            SimpleNamespace(client="claude-code", boundary="bwrap")))

    def test_the_drive_passes_the_roles_boundary_to_the_launch(self):
        from coordinator_core import drive
        pipeline = {"roles": {"worker": {"client": "claude-code", "model": "sonnet", "timeout": 60,
                                         "boundary": "bwrap"}}}
        with unittest.mock.patch.object(worker, "run_assignment", return_value="report") as run:
            self.assertEqual("report", drive.Launcher("repo", "store", "owner", pipeline).launch("a1", "worker"))
        self.assertEqual("bwrap", run.call_args.args[-1])


@unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
class SandboxTests(unittest.TestCase):
    """A real bwrap, synthetic main and linked worktrees, a probe that tries what L4 forbids."""

    PROBE = r'''
import json, os, subprocess, sys
ops = json.loads(sys.argv[1]); out = {}
for name, kind, path in ops:
    try:
        if kind == "read":
            open(path, "rb").read()
        elif kind == "write":
            with open(path, "a") as stream:
                stream.write("x\n")
        elif kind == "list":
            os.listdir(path)
        elif kind == "git":
            done = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True)
            if done.returncode:
                raise OSError(done.stderr.decode()[:200])
        out[name] = "ok"
    except Exception as exc:
        out[name] = type(exc).__name__
print(json.dumps(out))
'''

    def setUp(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        base = self.base = Path(self.tmp.name).resolve()
        self.main, self.linked = base / "main", base / "story"
        git(base, "init", "-q", str(self.main))
        (self.main / "a.txt").write_text("a\n")
        git(self.main, "add", "a.txt")
        git(self.main, "commit", "-qm", "a")
        git(self.main, "worktree", "add", "-q", "-b", "story", str(self.linked))
        self.common = self.main / ".git"
        coordinator = self.common / "coordinator"
        for rel in ("store.db", "runs/other/events.jsonl", "bundles/b1/artifact/a.txt"):
            (coordinator / rel).parent.mkdir(parents=True, exist_ok=True)
            (coordinator / rel).write_text("sealed\n")
        self.run_dir = coordinator / "runs" / "own"
        self.run_dir.mkdir()
        (self.run_dir / "probe.py").write_text(self.PROBE)
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("secret\n")
        self.bwrap, self.version = boundary.preflight(dict(os.environ))

    def sandbox(self, project, command, cwd):
        mounts, absent = boundary.ordered([*boundary.system_mounts(), Mount("tmpfs", str(Path.home())), *project])
        self.assertEqual([], absent)
        argv = boundary.wrap(self.bwrap, mounts, cwd, command, {})
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**os.environ, **boundary.ENV},
                              stdin=subprocess.DEVNULL)

    def probe(self, kind, tree, ops):
        sealed = (str(self.common / "coordinator/bundles/b1"),) if kind == "reviewer" else ()
        project = boundary.project_mounts(kind, self.run_dir, tree, tree, self.common, sealed=sealed)
        done = self.sandbox(project, ["python3", str(self.run_dir / "probe.py"), json.dumps(ops)], tree)
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def forbidden(self, tree):
        coordinator = self.common / "coordinator"
        return [("secret", "read", str(self.outside / "secret")), ("store", "read", str(coordinator / "store.db")),
                ("sibling", "read", str(coordinator / "runs/other/events.jsonl")),
                ("config", "write", str(self.common / "config")), ("hook", "write", str(self.common / "hooks/x")),
                ("index", "write", str(self.common / "index")),
                ("pointer", "write", str(tree / ".git")) if tree != self.main else
                ("head", "write", str(self.common / "HEAD")),
                ("worktrees", "write", str(self.common / "worktrees/story/HEAD"))]

    def test_writer_in_a_main_and_a_linked_worktree(self):
        for tree in (self.main, self.linked):
            with self.subTest(tree=tree.name):
                ops = [*self.forbidden(tree), ("edit", "write", str(tree / "a.txt")),
                       ("own", "write", str(self.run_dir / "out.txt")), ("status", "git", str(tree)),
                       ("bundle", "read", str(self.common / "coordinator/bundles/b1/artifact/a.txt"))]
                found = self.probe("writer", tree, ops)
                for name, *_ in self.forbidden(tree) + [("bundle",)]:
                    self.assertNotEqual("ok", found[name], name)
                self.assertEqual({"edit": "ok", "own": "ok", "status": "ok"},
                                 {key: found[key] for key in ("edit", "own", "status")})
        self.assertEqual("sealed\n", (self.common / "coordinator/store.db").read_text())
        self.assertNotIn("x", (self.common / "config").read_text().splitlines())

    def test_reviewer_reads_its_sealed_copy_and_writes_nothing_of_the_project(self):
        ops = [*self.forbidden(self.linked), ("edit", "write", str(self.linked / "a.txt")),
               ("bundle", "read", str(self.common / "coordinator/bundles/b1/artifact/a.txt")),
               ("own", "write", str(self.run_dir / "out.txt")), ("status", "git", str(self.linked))]
        found = self.probe("reviewer", self.linked, ops)
        for name, *_ in self.forbidden(self.linked) + [("edit",)]:
            self.assertNotEqual("ok", found[name], name)
        self.assertEqual({"bundle": "ok", "own": "ok", "status": "ok"},
                         {key: found[key] for key in ("bundle", "own", "status")})

    def test_isolated_sees_nothing_of_the_project(self):
        cwd, receipts = self.base / "scratch/cwd", self.base / "scratch/receipts"
        cwd.mkdir(parents=True)
        git(self.base, "init", "-q", str(cwd))
        receipts.mkdir()
        (receipts / "probe.py").write_text(self.PROBE)
        ops = [("main", "list", str(self.main)), ("secret", "read", str(self.outside / "secret")),
               ("cwd-git", "write", str(cwd / ".git/config")), ("cwd", "write", str(cwd / "x")),
               ("receipt", "write", str(receipts / "last.json"))]
        done = self.sandbox(boundary.project_mounts("isolated", receipts, cwd),
                            ["python3", str(receipts / "probe.py"), json.dumps(ops)], cwd)
        found = json.loads(done.stdout)
        self.assertEqual("ok", found.pop("receipt"))
        self.assertTrue(all(value != "ok" for value in found.values()), found)

    def test_a_copy_is_private_and_writable_and_the_host_file_unchanged(self):
        source = self.outside / "state.json"
        source.write_text("{}\n")
        copies = boundary.open_copies([Mount("copy", str(source))])
        try:
            mounts, _ = boundary.ordered([*boundary.system_mounts(), Mount("copy", str(source))])
            done = subprocess.run(boundary.wrap(self.bwrap, mounts, Path("/"), ["/bin/sh", "-c",
                                  f"stat -c %a {source}; cat {source}; echo more >> {source}; cat {source}"], copies),
                                  capture_output=True, text=True, timeout=60, pass_fds=tuple(copies.values()))
        finally:
            boundary.close(copies)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["600", "{}", "{}", "more"], done.stdout.split())
        self.assertEqual("{}\n", source.read_text())

    def test_an_empty_mount_hides_a_file_under_a_read_write_bind_and_never_writes_it(self):
        home = self.outside / "codex-home"
        home.mkdir()
        (home / "AGENTS.md").write_text("operator rules\n")
        inner = [Mount("rw", str(home)), Mount("empty", str(home / "AGENTS.md"))]
        copies = boundary.open_copies(inner)
        try:
            mounts, _ = boundary.ordered([*boundary.system_mounts(), *inner])
            script = f"wc -c < {home}/AGENTS.md; echo more >> {home}/AGENTS.md || echo refused; echo ok > {home}/x"
            done = subprocess.run(boundary.wrap(self.bwrap, mounts, Path("/"), ["/bin/sh", "-c", script], copies),
                                  capture_output=True, text=True, timeout=60, pass_fds=tuple(copies.values()))
        finally:
            boundary.close(copies)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(["0", "refused"], done.stdout.split())
        self.assertEqual("operator rules\n", (home / "AGENTS.md").read_text())
        self.assertEqual("ok\n", (home / "x").read_text())   # the parent bind still writes

    def marker_alive(self, marker):
        for pid in os.listdir("/proc"):
            try:
                if pid.isdigit() and marker.encode() in Path(f"/proc/{pid}/cmdline").read_bytes():
                    return True
            except OSError:
                continue
        return False

    def test_descendants_die_with_the_sandbox_setsid_ones_too(self):
        marker = f"3600.{os.getpid()}{int(time.time() * 1000) % 100000}"
        script = (f"sleep {marker}1 & python3 -c \"import os, subprocess; os.setsid(); "
                  f"subprocess.Popen(['sleep', '{marker}2'])\" & sleep 0.3; exit 0")
        mounts, _ = boundary.ordered(boundary.system_mounts())
        done = subprocess.run(boundary.wrap(self.bwrap, mounts, Path("/"), ["/bin/sh", "-c", script], {}),
                              capture_output=True, text=True, timeout=60, start_new_session=True)
        self.assertEqual(0, done.returncode, done.stderr)
        time.sleep(0.5)
        self.assertFalse(self.marker_alive(marker), "a descendant outlived the sandbox")

    def test_killing_bwrap_kills_what_runs_inside(self):
        marker = f"3700.{os.getpid()}{int(time.time() * 1000) % 100000}"
        mounts, _ = boundary.ordered(boundary.system_mounts())
        process = subprocess.Popen(boundary.wrap(self.bwrap, mounts, Path("/"), ["/bin/sh", "-c", f"sleep {marker}"],
                                                 {}), start_new_session=True)
        deadline = time.time() + 10
        while not self.marker_alive(marker) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marker_alive(marker))
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        deadline = time.time() + 5
        while self.marker_alive(marker) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.marker_alive(marker))

    def test_a_child_that_exits_at_once_and_the_probe(self):
        mounts, _ = boundary.ordered(boundary.system_mounts())
        done = subprocess.run(boundary.wrap(self.bwrap, mounts, Path("/"), ["/bin/true"], {}), capture_output=True,
                              timeout=60)
        self.assertEqual(0, done.returncode)
        boundary.probe(self.bwrap, mounts, Path("/"), ["/bin/true"], dict(os.environ), "true")
        with self.assertRaises(DomainError) as caught:
            boundary.probe(self.bwrap, mounts, Path("/"), ["/bin/false"], dict(os.environ), "false")
        self.assertIn("false fails inside it", caught.exception.message)
        self.assertTrue(self.version.startswith("bubblewrap"))


@unittest.skipIf(REASON and os.environ.get("COORDINATOR_REQUIRE_BWRAP") != "1", f"bwrap unusable: {REASON}")
class LaunchTests(CoreBase):
    """A real launch under the boundary: the fake worker (codex-cli by default), its receipts and their replay."""

    def setUp(self):
        if REASON:
            self.fail(f"COORDINATOR_REQUIRE_BWRAP=1 but bwrap is unusable: {REASON}")
        super().setUp()
        self.env["FAKE_COUNT"] = f"/tmp/fake-count-{os.getpid()}"   # inside the sandbox's own /tmp
        self.env["COORDINATOR_BOUNDARY_PASS"] = "FAKE_"   # the fake client's switches pass the allowlist
        self.codex_home = self.root / "codex-home"   # never the operator's own ~/.codex
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text("model = \"fake\"\n")
        self.env["CODEX_HOME"] = str(self.codex_home)

    def bounded(self, assignment, timeout=30, **env):
        with unittest.mock.patch.dict(os.environ, {**self.env, **env}):
            return worker.run_assignment(self.t.repo, self.store, self.owner, assignment, "fake-model", timeout,
                                         boundary_kind="bwrap")

    def test_a_worker_run_completes_records_its_mounts_and_replays(self):
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        self.assertEqual("r", (self.t.path / "result.md").read_text())
        launch = json.loads((Path(report.run_dir) / "launch.json").read_text())
        record = launch["boundary"]
        self.assertEqual([], boundary.check_record(record, "bwrap", launch["argv"]))
        modes = {item["path"]: item["mode"] for item in record["mounts"]}
        common = self.t.repo.common_dir
        self.assertEqual(("rw", "ro", "tmpfs", "rw"), (modes[str(self.t.path)], modes[str(common)],
                                                       modes[str(common / "coordinator")], modes[report.run_dir]))
        self.assertIn("GIT_OPTIONAL_LOCKS", record["env_names"])
        self.assertEqual("completed-claim", worker.inspect_run(self.t.repo, self.store, self.assign_id(report)).state)

    def assign_id(self, report):
        return json.loads((Path(report.run_dir) / "launch.json").read_text())["assignment_id"]

    def test_a_worker_cannot_write_git_metadata_or_read_the_store(self):
        common = self.t.repo.common_dir
        config = (common / "config").read_text()
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r"), (str(common / "config"), "x")))
        self.assertEqual("unknown", report.state)
        self.assertEqual(config, (common / "config").read_text())

    def test_an_isolated_run_sees_only_its_cwd_and_receipts(self):
        root = self.root / "isolated"
        cwd = root / "cwd"
        cwd.mkdir(parents=True)
        git(cwd, "init", "-q")
        envelope = {"role": "judge", "assignment_id": "judge-x", "request_fingerprint": "f" * 64}
        with unittest.mock.patch.dict(os.environ, self.env):
            result = clients.run_isolated("codex-cli", self.t.repo, root, cwd, root / "receipts", envelope,
                                          worker._schema(), "fake-model", 30, boundary_kind="bwrap")
        self.assertEqual("observed", result.state, result.needs)
        launch = json.loads((root / "receipts" / "launch.json").read_text())
        modes = {item["path"]: item["mode"] for item in launch["boundary"]["mounts"]}
        self.assertEqual(("ro", "rw"), (modes[str(cwd)], modes[str(root / "receipts")]))
        self.assertFalse(any(path.startswith(str(self.t.path)) or path.startswith(str(self.main))
                             for path in modes), "nothing of the project is mounted")

    def test_codex_global_instructions_are_empty_inside_and_kept_outside(self):
        (self.codex_home / "AGENTS.md").write_text("operator rules\n")
        (self.codex_home / "AGENTS.override.md").write_text("override\n")
        (self.codex_home / "RTK.md").write_text("notes\n")
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r"),
                                                               (str(self.codex_home / "AGENTS.md"), "more\n")))
        self.assertEqual("unknown", report.state)
        modes = {item["path"]: item["mode"] for item in
                 json.loads((Path(report.run_dir) / "launch.json").read_text())["boundary"]["mounts"]}
        self.assertEqual(("empty", "empty", "ro"), tuple(modes[str(self.codex_home / name)]
                                                         for name in ("AGENTS.md", "AGENTS.override.md", "RTK.md")))
        self.assertEqual("operator rules\n", (self.codex_home / "AGENTS.md").read_text())

    def test_a_persistent_client_entry_is_read_only(self):
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r"),
                                                               (str(self.codex_home / "config.toml"), "notify = 1\n")))
        self.assertEqual("unknown", report.state)
        self.assertEqual("model = \"fake\"\n", (self.codex_home / "config.toml").read_text())

    def test_a_new_persistent_client_entry_ends_unknown(self):
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r"),
                                                               (str(self.codex_home / "rules" / "x.rules"), "allow\n")))
        self.assertEqual("unknown", report.state)
        record = json.loads((Path(report.run_dir) / "integrity.json").read_text())
        self.assertIn(str(self.codex_home / "rules"), record["client_state_changed"])
        self.assertFalse((self.codex_home / "rules").exists(), "the created entry was moved out of the state")
        moved = record["client_state_quarantined"]
        self.assertEqual([str(self.codex_home / "rules")], [item["path"] for item in moved])
        self.assertTrue((Path(moved[0]["to"]) / "x.rules").is_file())
        self.assertTrue(Path(moved[0]["to"]).is_relative_to(self.t.repo.common_dir / "coordinator" / "quarantine"))

    def test_codex_sqlite_state_is_the_runs_own_and_the_shared_memories_stay_untouched(self):
        (self.codex_home / "memories_1.sqlite").write_bytes(b"operator memories\n")   # issue #5
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        own = Path(report.run_dir) / clients.CODEX_SQLITE
        self.assertEqual(["memories_1.sqlite", "memories_1.sqlite-shm", "memories_1.sqlite-wal", "state_5.sqlite"],
                         sorted(os.listdir(own)))
        self.assertEqual(["config.toml", "memories_1.sqlite"], sorted(os.listdir(self.codex_home)))
        self.assertEqual(b"operator memories\n", (self.codex_home / "memories_1.sqlite").read_bytes())
        record = json.loads((Path(report.run_dir) / "integrity.json").read_text())
        self.assertEqual([], record["client_state_changed"])
        self.assertIn("CODEX_SQLITE_HOME", json.loads((Path(report.run_dir) / "launch.json").read_text())["env_names"])

    def test_sidecars_created_in_the_shared_codex_home_end_unknown_and_the_cause_names_them(self):
        (self.codex_home / "memories_1.sqlite").write_bytes(b"operator memories\n")
        wal = self.codex_home / "memories_1.sqlite-wal"   # absent before the run: still fail-closed
        report = self.bounded(self.assign(), FAKE_WRITE=writes(("result.md", "r"), (str(wal), "a write\n")))
        self.assertEqual("unknown", report.state)
        self.assertEqual(["a persistent entry of the client's state changed or was created (memories_1.sqlite-wal); "
                          "the entries it created were moved to quarantine during the run (integrity.json); the "
                          "coordinator ran no Git command after it"], report.evidence_needs)
        self.assertFalse(wal.exists(), "the operator's next session never replays the run's write")
        self.assertEqual(b"operator memories\n", (self.codex_home / "memories_1.sqlite").read_bytes())

    def test_the_quarantine_follows_no_planted_link(self):
        decoy = self.root / "decoy"
        decoy.mkdir()
        env = {**os.environ, **self.env}
        before = clients.persistent_state("codex-cli", env)
        state = self.codex_home / "rules"
        state.mkdir()
        quarantine = self.root / "q" / "run"
        quarantine.parent.mkdir()
        quarantine.symlink_to(decoy)   # what a run could plant if the quarantine were writable inside
        changed, moved = clients.persistent_changes("codex-cli", env, before, quarantine)
        self.assertIn(str(state), changed)
        self.assertEqual([None], [item["to"] for item in moved])
        self.assertEqual([], list(decoy.iterdir()))
        self.assertTrue(state.is_dir(), "nothing was moved through the link")

    def test_the_environment_allowlist(self):
        env = {"PATH": "/p", "HOME": "/h", "LC_ALL": "C", "OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a",
               "GH_TOKEN": "g", "DBUS_SESSION_BUS_ADDRESS": "unix:abstract=x", "SSH_AUTH_SOCK": "/s", "FAKE_X": "1"}
        with unittest.mock.patch.dict(os.environ, {"COORDINATOR_BOUNDARY_PASS": ""}):
            kept, dropped = clients.boundary_passed(clients.CODEX, env)
        self.assertEqual({"PATH", "HOME", "LC_ALL", "OPENAI_API_KEY"}, set(kept))
        self.assertEqual(["ANTHROPIC_API_KEY", "DBUS_SESSION_BUS_ADDRESS", "FAKE_X", "GH_TOKEN", "SSH_AUTH_SOCK"],
                         dropped)
        kept, _ = clients.boundary_passed(clients.COPILOT, env)
        self.assertEqual({"PATH", "HOME", "LC_ALL", "GH_TOKEN"}, set(kept) - {"FAKE_X"})

    def test_a_flag_cannot_change_the_bindings_boundary(self):
        self.assertIsNone(worker._resolve_boundary(None, None))
        self.assertEqual("bwrap", worker._resolve_boundary("bwrap", None))
        self.assertEqual("bwrap", worker._resolve_boundary(None, {"boundary": "bwrap"}))
        self.assertEqual("bwrap", worker._resolve_boundary("bwrap", {}))
        with self.assertRaises(DomainError):
            worker._resolve_boundary("docker", {"boundary": "bwrap"})

    def test_replay_refuses_a_missing_or_altered_record(self):
        argv = ["codex", "exec"]
        good = {"argv": argv, "boundary": boundary.record("/usr/bin/bwrap", "v", [], [], {}, ["/usr/bin/bwrap", "--",
                                                                                              *argv])}
        worker._check_boundary(good, {"boundary": "bwrap"})
        worker._check_boundary({"argv": argv}, None)
        for launch, binding in (({"argv": argv}, {"boundary": "bwrap"}),
                                ({**good, "boundary": {**good["boundary"], "argv": ["/usr/bin/bwrap", "--", "x"]}},
                                 {"boundary": "bwrap"}),
                                ({**good, "boundary": {**good["boundary"], "kind": "other"}}, None)):
            with self.assertRaises(DomainError) as caught:
                worker._check_boundary(launch, binding)
            self.assertEqual("UNKNOWN_EVIDENCE", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
