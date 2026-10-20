"""Closed registry of one-attempt worker clients and the client-neutral process seams around them.

A client entry renders argv from a LaunchSpec and turns its own retained raw output into data: one thread id, the
final bytes and client facts. The core keeps every classification (provenance, delta, sealed-evidence integrity,
fence, independence, acceptance). See docs/claude-adapter-design.md sections 3, 4 and 14, D16 and D17."""

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .model import DomainError
from .repo import RepoIdentity
from .run_receipt import canonical, delta, inventory, private_file, read_binding, seal, stamp_json

DEFAULT = "codex-cli"
ENVELOPE_ARG = "@envelope.json"  # argv placeholder; the runner substitutes the envelope text at spawn (T2)
WORKER_RETURN = "worker-return"
MAX_RETURN_BYTES = 1 << 20
MAX_DEPTH = 64
GROUP_WAIT = 2.0  # seconds per drain step (N2)
SEALED_TOPS = ("bindings", "bundles", "judgments", "pipelines", "drives", "resolutions",  # D16; driver folders last
               "story")
ENDED = ("end.json", "timeout.json", "interrupted.json")
RUNNING_SEALED = ("envelope.json", "return.schema.json", "launch-intent.json", "launch.json")
# A parent agent session's variables (a Claude Code session's CLAUDE*/CLAUDECODE and messaging socket and token, other
# vendors' ANTHROPIC* credentials, and the markers it sets for its own tools) never reach a non-Claude worker; Claude
# keeps its own scrub (env_unset, KEEP_ENV) and its ANTHROPIC* variables, as before.
PARENT_MARKERS = frozenset({"CLAUDE*", "ANTHROPIC*", "AI_AGENT", "GIT_EDITOR", "COREPACK_ENABLE_AUTO_PIN",
                            "NoDefaultCurrentDirectoryInExePath"})


@dataclass(frozen=True)
class LaunchSpec:      # decided by the coordinator; a client only renders it
    binary: str
    mode: str
    worktree: Path
    run_dir: Path
    schema: dict
    model: str
    max_turns: int | None
    session_id: str | None
    read_dirs: tuple[str, ...]
    commands: tuple[str, ...]                    # exact author Bash prefixes from the binding, else ()
    isolated: bool = False                       # D17: read-only, no Bash, no add-dir
    return_path: str | None = None               # worker-file clients only; also named in the envelope
    web: bool = False                            # design D23: a story worker may use the network


@dataclass(frozen=True)
class Terminal:        # replayable from retained bytes; facts["tools"] lists reported tool names when reported
    thread_id: str
    final: bytes
    facts: dict
    stop: dict | None = None   # design D25: a client stop with a known cause and no return ({"kind", "num_turns"})


@dataclass(frozen=True)
class Client:
    name: str
    executable: str
    confinement: str
    presets_session: bool
    return_source: str                           # "client-file" | "stdout" | "worker-file" (T1)
    envelope_via: str                            # "stdin" | "argv"; argv uses ENVELOPE_ARG (T2)
    max_envelope_bytes: int | None               # checked before reserve_launch (T3)
    write_free_readonly: bool                    # False: no reviewer or isolated launch (T6)
    max_turns_default: int | None                # None: max_turns is refused
    env: dict[str, str]                          # static env; also used by preflight; names recorded
    env_unset: frozenset[str]                    # removed from the inherited env (an entry `PREFIX*` removes every
                                                 # name with that prefix except env_keep); names recorded (T4)
    run_subdirs: tuple[str, ...]                 # created empty, 0700, in the run dir before spawn (T4)
    private_subdirs: tuple[str, ...]             # subset of run_subdirs excluded from other runs' D16 baselines
    observation_name: str | None                 # sealed post-run observation, e.g. "freshness.json"
    internal_tools: frozenset[str]               # tool names the client reports beyond those requested
    run_env: Callable[[LaunchSpec], dict[str, str]]
    allowed_tools: Callable[[str], frozenset[str]] | None  # mode -> requested | internal_tools; None: no check
    argv: Callable[[LaunchSpec], list[str]]
    launch_facts: Callable[[LaunchSpec], dict]   # taken before the spawn (Copilot records pre_spawn_at)
    observe: Callable[[Path, dict], dict] | None  # once, after the group drain; sealed as observation_name
    terminal: Callable[[Path, dict], Terminal]   # pure function of sealed or digest-bound run-dir bytes
    env_keep: frozenset[str] = frozenset()       # documented auth names a `PREFIX*` scrub keeps for this client
    session_scrub: frozenset[str] = frozenset()  # parent-session markers removed like env_unset but recorded in the
                                                 # env-scrub.json sidecar, so launch.json bytes stay unchanged (Codex)
    script_launcher: bool = False                # a bound author runs its commands through the coordinator-written
                                                 # RUN/worker-tools/bmad-script (Copilot, design section 14)


@dataclass(frozen=True)
class IsolatedRun:
    state: str                                   # "observed" | "unknown"
    source_id: str | None
    final: dict | None
    receipt_dir: str
    launch_sha256: str | None
    return_sha256: str | None
    needs: list[str]


def _unknown(message: str) -> DomainError:
    return DomainError("UNKNOWN_EVIDENCE", message)


def jsonl_records(path: Path) -> list[dict]:
    """Every line one JSON object with a string type; anything else raises ValueError or OSError."""
    seen = []
    for line in path.read_bytes().splitlines():
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("invalid JSONL event")
        seen.append(item)
    return seen


def _codex_thread(events: Path) -> str:
    try:
        seen = jsonl_records(events)
        starts = [i for i, item in enumerate(seen) if item["type"] == "thread.started"]
        terminals = [i for i, item in enumerate(seen) if item["type"] in ("turn.completed", "turn.failed")]
        if len(starts) != 1 or len(terminals) != 1 or starts[0] >= terminals[0] or \
           seen[terminals[0]]["type"] != "turn.completed" or terminals[0] != len(seen) - 1:
            raise ValueError("missing or contradictory terminal event")
        thread = seen[starts[0]].get("thread_id")
        if not isinstance(thread, str) or not thread:
            raise ValueError("missing thread ID")
        if any(item.get("thread_id") not in (None, thread) for item in seen):
            raise ValueError("different thread ID")
        return thread
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _unknown("Worker terminal event is missing or malformed") from exc


def _codex_terminal(run_dir: Path, launch: dict) -> Terminal:
    thread = _codex_thread(run_dir / "events.jsonl")
    try:
        return Terminal(thread, (run_dir / "last.json").read_bytes(), {})
    except OSError as exc:
        raise _unknown("Worker final message is missing") from exc


def _codex_argv(spec: LaunchSpec) -> list[str]:
    sandbox = ["--sandbox", "read-only", "--skip-git-repo-check"] if spec.isolated else ["--sandbox", spec.mode]
    network = ["-c", CODEX_NETWORK] if spec.web and not spec.isolated and spec.mode != "read-only" else []
    return [spec.binary, "exec", "-", "-C", str(spec.worktree), *sandbox, "-c", "approval_policy=never", *network,
            "--json", "--output-schema", str(spec.run_dir / "return.schema.json"),
            "--output-last-message", str(spec.run_dir / "last.json"), "-m", spec.model]


CODEX = Client(
    name="codex-cli", executable="codex", confinement="os-sandbox-requested", presets_session=False,
    return_source="client-file", envelope_via="stdin", max_envelope_bytes=None, write_free_readonly=True,
    max_turns_default=None, env={}, env_unset=frozenset(), run_subdirs=(), private_subdirs=(),
    observation_name=None, internal_tools=frozenset(), run_env=lambda spec: {}, allowed_tools=None,
    argv=_codex_argv, observe=None, terminal=_codex_terminal,
    launch_facts=lambda spec: {"web": True} if spec.web and not spec.isolated and spec.mode != "read-only" else {},
    session_scrub=PARENT_MARKERS)  # Codex auth (OPENAI_*, CODEX_*) is outside these patterns and passes


# Claude Code `claude -p` (design sections 5-9). Observed in the live smokes (2.1.289 and 2.1.290, attempts 1-8; design
# section 13):
# -p with the prompt on stdin, --output-format stream-json --verbose (record shapes), --session-id echoed with
# --no-session-persistence, --json-schema (both profile schemas), --model, --max-turns, --setting-sources "",
# --strict-mcp-config/--mcp-config (empty mcp_servers), --tools (init tools plus StructuredOutput),
# --permission-mode acceptEdits|dontAsk, --permission-prompts none, both --allowedTools forms, --add-dir reads, one
# deny rule firing and a ` *` rule matching a bare call. Still docs-only: whether `mcp__*` survives the merged
# --disallowedTools, env-prefix, `&&` and `$( )` matching, DISABLE_AUTOUPDATER, CLAUDE_CODE_DISABLE_BACKGROUND_TASKS
# and CLAUDE_CODE_DISABLE_AUTO_MEMORY, and whether a shell variable is expanded before matching. Some read-only Bash
# commands may be auto-allowed without any rule (the coordinator never relies on it), so the tool policy never limits
# reads; it is advisory, never a sandbox.
ISOLATION = ["--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--disallowedTools", "mcp__*"]
AUTHOR_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
WEB_TOOLS = "WebFetch,WebSearch"                 # design D23: appended to AUTHOR_TOOLS for a web worker
WEB_RULES = ["WebFetch", "WebSearch"]            # design D23: bare allow rules (WebSearch takes no specifier)
CODEX_NETWORK = "sandbox_workspace_write.network_access=true"   # design D23: outbound network in workspace-write
REVIEW_TOOLS = "Read,Grep,Glob,Bash"
REVIEW_ALLOWED = "Read,Grep,Glob,Bash(sha256sum *)"
ISOLATED_TOOLS = ISOLATED_ALLOWED = "Read,Grep,Glob"
INHERITED = ("CLAUDE", "ANTHROPIC")             # inherited variables recorded by name, never by value
# Generic env-scrub hook: a `PREFIX*` entry in env_unset removes every inherited name with that prefix except these
# documented authentication and provider variables (Claude Code env-vars docs), so env-based auth keeps working:
# the credential directory, OAuth tokens, provider switches, provider-auth skips and mTLS client credentials.
KEEP_ENV = frozenset({
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH", "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"})
SEALED_ARG = re.compile(r"[A-Za-z0-9._/-]+")
# Author deny rules for the contract and protected paths, so an allowed `uv run` prefix cannot run a script the
# author just rewrote. They continue the single `--disallowedTools` that ends ISOLATION (the flag never repeats).
# Advisory, like every rule; the cwd-relative gitignore-style path syntax is docs-only. The contract digests
# (which cover the whole `_bmad/scripts` tree) are rechecked at return either way.
PROTECTED = ("_bmad/**", ".agents/**", ".claude/**", ".bmad-coordinator/**", "AGENTS.md", "CLAUDE.md")
AUTHOR_DENIED = [f"{tool}({path})" for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") for path in PROTECTED]
PATTERN_META = frozenset("*?[]{},():")  # `:` also separates Copilot's `shell(<prefix>:*)`
MAX_COMMANDS, MAX_COMMAND = 8, 4096


def pattern_safe(text: str) -> bool:
    """No glob or permission-pattern metacharacter and no control character, so text can sit inside a rule."""
    return type(text) is str and not any(char in PATTERN_META or ord(char) < 32 or ord(char) == 127 for char in text)


def bound_commands(binding: dict | None) -> tuple[str, ...]:
    """The exact author Bash prefixes sealed in a binding (absent: none); a malformed value is refused before
    reservation, never read loosely."""
    value = binding.get("commands", []) if binding else []
    if not isinstance(value, list) or len(value) > MAX_COMMANDS or \
       any(type(item) is not str or not item or len(item) > MAX_COMMAND or not pattern_safe(item) for item in value):
        raise DomainError("INVALID_INPUT", "Binding names malformed author commands")
    return tuple(value)


def author_rules(spec: LaunchSpec) -> list[str]:
    """Design section 5: one `Bash(<prefix> *)` per exact binding prefix plus the approved stdin-only
    `Bash(sha256sum)`; a generic assignment gets none. A ` *` rule matched a bare call and a pipe matched two rules in
    the smoke; other matching is docs-only. Rules are advisory (some read-only commands may run without any rule; the
    coordinator never relies on it): the inventory delta, the contract digests and D16 are the audit."""
    return [f"Bash({command} *)" for command in spec.commands] + ["Bash(sha256sum)"] if spec.commands else []


UV_PREFIX = "uv run --no-project "
LAUNCHER_DIR, LAUNCHER_NAME, LAUNCHER_CHECK = "worker-tools", "bmad-script", "launcher-check.json"
SHEBANG_SAFE = re.compile(r"/[^\s\x00-\x1f\x7f]+")  # one shebang token; at most 120 UTF-8 bytes (launcher_bytes)
MAX_LAUNCHER = 1 << 16
# Design section 14, rulings R3, R3a, fix rounds 2-3 and the security round (Copilot live probe 1): Copilot approves
# shell commands by bare command name, so no prefix rule can confine `uv`. The runner writes this stdlib launcher into
# the run directory. The coordinator's interpreter runs it isolated and without `site` (-IS: no PYTHON* variable, site
# directory, user site or script directory is read; only the dynamic loader still reads LD_* first), and it execs the
# absolute `uv` resolved at launch as `uv run --no-config --no-project --python <that interpreter>` (no uv.toml or
# pyproject.toml the author may have written is read, and uv never discovers a worktree or parent .venv; an
# interpreter under the worktree or common directory is refused by launcher_tools before reservation), in
# the worktree, with an environment of only the PATH and HOME sealed at launch and PYTHONDONTWRITEBYTECODE=1, so no
# XDG_*, LD_*, PYTHON*, UV_*, token or other inherited variable reaches uv or the script. It runs only when its
# arguments start with one bound form (the binding command without the uv prefix, token for token) and no further
# argument is a short option or repeats or abbreviates an option of that form; otherwise it exits 2 without running
# anything.
LAUNCHER = """#!__PYTHON__ -IS
\"\"\"bmad-script: written by the coordinator for one run (design section 14). It runs one bound BMAD script form
through `uv run --no-config --no-project --python` and nothing else; anything else exits 2 without running anything.
\"\"\"
import os
import sys

FORMS = __FORMS__
UV, PYTHON, CWD, PATH, HOME = __UV__, __INTERPRETER__, __CWD__, __PATH__, __HOME__
ARGS = sys.argv[1:]
for FORM in FORMS:
    if tuple(ARGS[:len(FORM)]) == FORM:
        REST = ARGS[len(FORM):]
        NAMES = [token.split("=", 1)[0] for token in REST if token.startswith("--")]
        if any(token[:1] == "-" and token[1:2].isalpha() for token in REST) or \\
           any(len(name) > 2 and option.startswith(name) for name in NAMES for option in FORM
               if option.startswith("--")):
            break
        ENV = {"PATH": PATH, "HOME": HOME, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            os.chdir(CWD)
            os.execve(UV, [UV, "run", "--no-config", "--no-project", "--python", PYTHON, *ARGS], ENV)
        except OSError as exc:
            sys.stderr.write("bmad-script: cannot run uv: %s\\n" % exc)
            sys.exit(127)
sys.stderr.write("bmad-script: refused, nothing was run: the arguments must start with one bound form, token for "
                 "token, with your arguments only after it, no short option and no option of the form repeated or "
                 "abbreviated:\\n" + "".join("  bmad-script " + " ".join(FORM) + " ...\\n" for FORM in FORMS))
sys.exit(2)
"""


def launcher_forms(commands: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """The launcher's forms: each bound `uv run --no-project <absolute .py> ...` command without the uv prefix, split
    on single spaces. Anything else (no commands, another prefix, an empty token, a relative or non-.py script) is
    refused before reservation, never bound loosely."""
    forms = tuple(tuple(command[len(UV_PREFIX):].split(" ")) for command in commands
                  if type(command) is str and command.startswith(UV_PREFIX))
    if not forms or len(forms) != len(commands) or \
       any("" in form or not form[0].startswith("/") or not form[0].endswith(".py") for form in forms):
        raise DomainError("INVALID_INPUT", "Binding names commands the script launcher cannot bind")
    return forms


def launcher_tools(roots: tuple = ()) -> dict:
    """What the launcher seals at launch: the coordinator's own interpreter (its shebang and uv's `--python`), the
    absolute `uv` on its PATH, and that PATH and HOME. No token is passed on (security round: uv and the scripts need
    none; the launch facts keep the names). Refused before reservation when the interpreter or uv cannot be bound, or
    when the interpreter lies under one of `roots` (the feature worktree and its common directory: a venv there has
    worker-writable `.pth` files and `pyvenv.cfg`; return-schema round, m1)."""
    found = shutil.which("uv", path=os.environ.get("PATH", os.defpath))
    tools = {"python": sys.executable or "", "uv": os.path.abspath(found) if found else "",
             "path": os.environ.get("PATH", os.defpath), "home": os.environ.get("HOME") or os.path.expanduser("~")}
    _bindable(tools)
    python = os.path.abspath(tools["python"])  # also with only its directory resolved: a venv behind a linked parent
    places = {python, os.path.realpath(python),
              os.path.join(os.path.realpath(os.path.dirname(python)), os.path.basename(python))}
    tops = {item for root in roots for item in (os.path.abspath(root), os.path.realpath(root))}
    if any(Path(place).is_relative_to(top) for place in places for top in tops):
        raise DomainError("BLOCKED_EVIDENCE", "Script launcher interpreter lies under the worktree or common directory")
    return tools


def _bindable(tools: dict) -> None:
    try:
        size = len(tools["python"].encode("utf-8"))  # a lone surrogate (non-UTF-8 path) cannot sit in a shebang
    except UnicodeEncodeError:
        size = None
    if size is None or size > 120 or not SHEBANG_SAFE.fullmatch(tools["python"]) or \
       not os.path.isabs(tools["uv"]) or not os.path.isabs(tools["home"]):
        raise DomainError("BLOCKED_EVIDENCE", "Script launcher interpreter or uv cannot be bound")


def launcher_bytes(commands: tuple[str, ...], tools: dict, cwd: str) -> bytes:
    """Deterministic launcher bytes for the bound commands, the sealed tools and the working directory, so the launch
    receipt identifies them by digest. Every placeholder is filled in one pass, so no sealed value is rewritten."""
    _bindable(tools)
    if not os.path.isabs(cwd):
        raise DomainError("BLOCKED_EVIDENCE", "Script launcher interpreter or uv cannot be bound")
    values = {"PYTHON": tools["python"], "INTERPRETER": repr(tools["python"]), "FORMS": repr(launcher_forms(commands)),
              "UV": repr(tools["uv"]), "CWD": repr(str(cwd)), "PATH": repr(tools["path"]), "HOME": repr(tools["home"])}
    return re.sub(r"__(PYTHON|INTERPRETER|FORMS|UV|CWD|PATH|HOME)__", lambda match: values[match.group(1)],
                  LAUNCHER).encode("utf-8")


def launcher_path(run_dir: Path) -> Path:
    return Path(run_dir) / LAUNCHER_DIR / LAUNCHER_NAME


def write_launcher(run_dir: Path, data: bytes) -> str:
    """Create RUN/worker-tools (0700) and the launcher (0500), exclusively and without following links; its digest."""
    path = launcher_path(run_dir)
    path.parent.mkdir(mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500)
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o500)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(data).hexdigest()


def launcher_state(run_dir: Path) -> dict:
    """After the drain (fix round 2, I2): the RUN/worker-tools listing and the launcher's kind, mode, link count and
    digest, read without following a link at any step; sealed by the runner as launcher-check.json."""
    state = {"entries": _listing(Path(run_dir), LAUNCHER_DIR), "regular": None, "mode": None, "links": None,
             "sha256": None}
    try:
        fd = _open_nofollow(Path(run_dir), LAUNCHER_DIR, LAUNCHER_NAME)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            state.update(regular=stat.S_ISREG(info.st_mode), mode=stat.S_IMODE(info.st_mode), links=info.st_nlink)
            data = stream.read(MAX_LAUNCHER + 1) if state["regular"] else b""
            if state["regular"] and len(data) <= MAX_LAUNCHER:
                state["sha256"] = hashlib.sha256(data).hexdigest()
    except OSError:
        pass
    return state


def launcher_intact(state: object, sha256: object) -> bool:
    """Exactly the one launcher the runner wrote: the sole entry, a single-link regular 0500 file with that digest."""
    return isinstance(sha256, str) and state == {"entries": [LAUNCHER_NAME], "regular": True, "mode": 0o500,
                                                 "links": 1, "sha256": sha256}


def _claude_argv(spec: LaunchSpec) -> list[str]:
    argv = [spec.binary, "-p", "--output-format", "stream-json", "--verbose", "--session-id", spec.session_id,
            "--no-session-persistence", "--model", spec.model, "--max-turns", str(spec.max_turns),
            "--json-schema", canonical(spec.schema).decode(), *ISOLATION]
    if spec.isolated:
        return argv + ["--tools", ISOLATED_TOOLS, "--permission-mode", "dontAsk", "--allowedTools", ISOLATED_ALLOWED]
    if spec.mode == "read-only":
        argv += ["--tools", REVIEW_TOOLS, "--permission-mode", "dontAsk", "--allowedTools", REVIEW_ALLOWED]
        for directory in spec.read_dirs:
            argv += ["--add-dir", directory]
        return argv
    rules = author_rules(spec)  # AUTHOR_DENIED adds values to the `--disallowedTools mcp__*` that ends ISOLATION
    tools = AUTHOR_TOOLS + ("," + WEB_TOOLS if spec.web else "")
    if spec.web:   # design D23
        rules = rules + WEB_RULES
    return argv + [*AUTHOR_DENIED, "--tools", tools, "--permission-mode", "acceptEdits",
                   "--permission-prompts", "none", *(["--allowedTools", *rules] if rules else [])]


def _claude_launch_facts(spec: LaunchSpec) -> dict:
    removed = removed_env(CLAUDE)
    inherited = sorted(name for name in os.environ if name.startswith(INHERITED) and name not in removed)
    facts = {"requested_mode": spec.mode, "confinement": CLAUDE.confinement,
             "isolation": {"flags": ISOLATION, "env": dict(CLAUDE.env), "inherited_names": inherited,
                           "removed_names": removed, "kept_names": kept_env(CLAUDE)},
             "session_id": spec.session_id, "max_turns": spec.max_turns}
    if spec.web and not spec.isolated and spec.mode != "read-only":   # design D23
        facts["web"] = True
    return facts


def _flag(argv: list, name: str) -> str:
    value = argv[argv.index(name) + 1]
    if type(value) is not str:
        raise ValueError("recorded flag value is not a string")
    return value


def _blocks(item: dict, kind: str) -> list[dict]:
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind] \
        if isinstance(content, list) else []


def _sealed_arg(word: str, root: str) -> bool:
    return bool(SEALED_ARG.fullmatch(word)) and word.startswith(root.rstrip("/") + "/") and \
        ".." not in word.split("/")


def _bundle_read_basis(seen: list[dict], launch: dict) -> str | None:
    """Design section 6 reviewer Bash audit, downgrade only. `command` only when every Bash tool_use, denied or
    not, is a bare `sha256sum` over safe paths under the sealed root and every sealed file was hashed with a
    non-error result; otherwise `claim`. Null unless the run is a bound read-only attempt. The sealed root and files
    come from the binding the launch receipt names by digest; the basis never makes `bundle_read` more than a claim."""
    digest = launch.get("binding_sha256")
    if launch.get("requested_mode") != "read-only" or not digest:
        return None
    binding = read_binding(Path(launch["common_dir"]) / "coordinator", f"bindings/{digest}.json", digest)
    root, files = binding.get("sealed_root"), binding.get("sealed_files")
    sealed = {item.get("sealed_path") for item in files if isinstance(item, dict)} if isinstance(files, list) else set()
    if not isinstance(root, str) or not sealed or not all(isinstance(path, str) for path in sealed):
        return "claim"
    results = {block["tool_use_id"]: block.get("is_error") is not True for item in seen if item["type"] == "user"
               for block in _blocks(item, "tool_result") if isinstance(block.get("tool_use_id"), str)}
    hashed = set()
    for item in seen:
        if item["type"] != "assistant":
            continue
        for block in _blocks(item, "tool_use"):
            if block.get("name") != "Bash":
                continue
            given = block.get("input")
            command = given.get("command") if isinstance(given, dict) else None
            words = command.split(" ") if isinstance(command, str) else []
            if len(words) < 2 or words[0] != "sha256sum" or not all(_sealed_arg(word, root) for word in words[1:]):
                return "claim"
            if isinstance(block.get("id"), str) and results.get(block["id"]) is True:
                hashed.update(words[1:])
    return "command" if sealed <= hashed else "claim"


def _claude_terminal(run_dir: Path, launch: dict) -> Terminal:
    """Design section 6 over the retained stream and the sealed launch receipt (plus, for a bound read-only run, the
    digest-checked binding it names): every line one JSON object with a string type; exactly one init, first;
    exactly one result, last; every session_id the preset; a successful result with an object structured_output.
    Init cwd and permissionMode must match when present (absence is a gap); init tools must lie in the requested
    --tools, internal_tools and mcp__* names (MCP names and servers are isolation gaps). Design D25: a result with
    subtype `error_max_turns` and terminal_reason `max_turns` is a client stop: an empty final and `stop` set."""
    try:
        seen = jsonl_records(run_dir / "events.jsonl")
        session, argv = launch.get("session_id"), launch.get("argv")
        if not isinstance(session, str) or not isinstance(argv, list):
            raise ValueError("launch receipt lacks the preset session or argv")
        inits = [index for index, item in enumerate(seen)
                 if item["type"] == "system" and item.get("subtype") == "init"]
        results = [index for index, item in enumerate(seen) if item["type"] == "result"]
        if inits != [0] or results != [len(seen) - 1] or len(seen) < 2:
            raise ValueError("stream lacks one init first or one result last")
        init, result = seen[0], seen[-1]
        if init.get("session_id") != session or result.get("session_id") != session or \
           any("session_id" in item and item["session_id"] != session for item in seen):
            raise ValueError("stream session differs from the preset")
        stopped = result.get("subtype") == "error_max_turns" and result.get("terminal_reason") == "max_turns"
        if not stopped and (result.get("subtype") != "success" or result.get("is_error") is not False or
                            not isinstance(result.get("structured_output"), dict)):
            raise ValueError("result is not a successful structured return")
        gaps = []
        expected = {"cwd": launch.get("cwd", launch.get("worktree")),
                    "permissionMode": _flag(argv, "--permission-mode")}
        for key, value in expected.items():
            if key not in init:
                gaps.append(f"init-{key}-absent")
            elif init[key] != value:
                raise ValueError(f"init {key} differs from the launch")
        tools = init.get("tools")
        if "tools" in init:
            if not isinstance(tools, list) or any(type(name) is not str or not name for name in tools):
                raise ValueError("init reports an unnamed tool")
            mcp = [name for name in tools if name.startswith("mcp__")]
            if set(tools) - set(mcp) - set(_flag(argv, "--tools").split(",")) - CLAUDE.internal_tools:
                raise ValueError("init reports a tool outside the allowlist")
            gaps += [f"mcp-tool:{name}" for name in mcp]
        servers = init.get("mcp_servers")
        if servers is not None and not isinstance(servers, list):
            raise ValueError("init mcp_servers is malformed")
        gaps += ["mcp-server:" + (item["name"] if isinstance(item, dict) and isinstance(item.get("name"), str)
                                  else canonical(item).decode()) for item in servers or ()]
        usage = result.get("modelUsage")
        facts = {"permission_denials": result.get("permission_denials"), "num_turns": result.get("num_turns"),
                 "terminal_reason": result.get("terminal_reason"), "stop_reason": result.get("stop_reason"),
                 "models": sorted(usage) if isinstance(usage, dict) else None,
                 "total_cost_usd": result.get("total_cost_usd"),
                 "init": {key: init.get(key) for key in ("tools", "mcp_servers", "permissionMode", "cwd", "model")},
                 "isolation_gaps": sorted(gaps), "bundle_read_basis": _bundle_read_basis(seen, launch)}
        checked = [name for name in tools if not name.startswith("mcp__")] if "tools" in init else []
        if checked:  # the names tool_check reads; MCP names are gaps, and an empty list is no tool information
            facts["tools"] = checked
        if stopped:   # design D25: the stream decides; the exit code is recorded by the runner
            turns = result.get("num_turns")
            return Terminal(session, b"", facts,
                            {"kind": "max-turns", "num_turns": turns if type(turns) is int else None})
        return Terminal(session, canonical(result["structured_output"]), facts)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise _unknown("Claude stream terminal record is missing or malformed") from exc


CLAUDE_REQUESTED = {"read-only": frozenset(REVIEW_TOOLS.split(",")),
                    "workspace-write": frozenset(AUTHOR_TOOLS.split(","))}
CLAUDE = Client(
    name="claude-code", executable="claude", confinement="tool-policy", presets_session=True,
    return_source="stdout", envelope_via="stdin", max_envelope_bytes=None, write_free_readonly=True,
    max_turns_default=60, env={"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                               "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},  # auto memory: docs-only, P0 checks it
    # the parent session's CLAUDE*/CLAUDECODE variables and the markers it sets for its own tools never reach the
    # worker (removed by name and recorded); other inherited variables pass
    env_unset=frozenset({"CLAUDE*", "AI_AGENT", "GIT_EDITOR", "COREPACK_ENABLE_AUTO_PIN",
                         "NoDefaultCurrentDirectoryInExePath"}),
    run_subdirs=(), private_subdirs=(), observation_name=None,
    internal_tools=frozenset({"StructuredOutput"}),  # P0 init (2.1.289, --json-schema); set only by a reviewed change
    run_env=lambda spec: {}, allowed_tools=lambda mode: CLAUDE_REQUESTED[mode] | CLAUDE.internal_tools,
    argv=_claude_argv, launch_facts=_claude_launch_facts, observe=None, terminal=_claude_terminal, env_keep=KEEP_ENV)


# GitHub Copilot CLI `copilot -p` (design section 14), author-only. Observed in the P0c probe (1.0.91, smoke-copilot-1):
# -p with the prompt as an argument, --output-format json (JSONL ending in a `result` record with sessionId, exitCode
# and usage), --session-id with a caller-chosen id echoed, -C, --model (an unavailable name exits 1 with no result),
# --add-dir write approval, --allow-tool write, `write(<path>)` deny rules (honoured, C15), --no-ask-user,
# --no-auto-update, --log-dir, --no-custom-instructions, --disable-builtin-mcps, --disallow-temp-dir and `--version`;
# --allow-all-tools is not needed. Not matched live: a multiword `shell(<prefix>:*)` rule (Copilot approves by bare
# command name, so a bound author runs its scripts through the coordinator's launcher, ruling R3). Smoke 2 (P0c with
# the launcher): `shell(bmad-script)` approves only the bare-name call (a full-path call is matched as that path and
# denied, so only the bare name is offered), the shell sees the prepended PATH, and --available-tools narrowed the
# offered set to internal_tools. Shell runs with the user's access: the approval policy is advisory, never a sandbox.
COPILOT_ISOLATION = ["--no-ask-user", "--no-auto-update", "--no-custom-instructions", "--disable-builtin-mcps",
                     "--disallow-temp-dir"]
# Author write deny rules, so `--allow-tool write` cannot rewrite a script that an allowed `shell(...)` prefix then
# runs (Claude's AUTHOR_DENIED, plus `.github/**`, where Copilot finds instructions, agents, skills and hooks). The
# `write(path)` kind is help-documented (1.0.91: a relative path matches by trailing components; denials beat every
# allow rule; shell invocations are not write tools); `**` subtree matching is not documented. Advisory until P0c shows
# a denied write under `_bmad/`; the contract digests rechecked at return stay the fail-closed backstop.
COPILOT_DENIED = [f"write({path})" for path in (*PROTECTED, ".github/**")]
COPILOT_TOKENS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")  # documented headless precedence (docs)
COPILOT_INHERITED = ("COPILOT", "GH_", "GITHUB")  # inherited variables recorded by name, never by value
# Tools of the bundle's "edit" category (1.0.91 app.js tool table: edit, create, str_replace_editor, apply_patch);
# only a tool.execution_start of one of these naming return_path counts as the return write. Bundle-read, not observed.
COPILOT_WRITE_TOOLS = frozenset({"create", "edit", "str_replace_editor", "apply_patch"})
# Rulings R2 and R2a: the author tool set. P0c offered the model 21 tools (smoke-copilot-1 `session.usage_checkpoint`,
# `--disable-builtin-mcps`; the stream has no init record). This reviewed set keeps 11 of them and drops every tool
# that reaches the network or spawns or drives agents or workflows: web_fetch, fetch_copilot_cli_documentation (its
# source is unverified), task, search_code_subagent, read_agent, list_agents, write_agent, run_dynamic_workflow and
# dynamic_workflows_manage; it also drops session_store_sql (querying past sessions is an account-context leak route,
# the class of Claude's auto memory). It is internal_tools and the `--available-tools` pin (help: "Only these tools
# will be available to the model"; smoke 2 saw the offered set equal it). A reported or offered tool outside it is
# `unknown`.
COPILOT_TOOLS = frozenset({"bash", "read_bash", "stop_bash", "list_bash", "view", "create", "edit", "grep", "glob",
                           "skill", "sql"})
COPILOT_LOCKS = ".session-operation-locks"  # P0c: Copilot always creates <id>.lock here beside the session (R1)
COPILOT_SKILL_DIRS = (".agents/skills",)    # P0c: listed as project skills in the system prompt (an isolation gap)
COPILOT_TOKEN_WARNING = ("COPILOT_GITHUB_TOKEN is not set: headless auth falls back to GH_TOKEN, GITHUB_TOKEN or a "
                         "stored credential, which a fresh per-run COPILOT_HOME may not hold (design section 14, C2)")
MAX_WORKSPACE_BYTES = 1 << 16


def _copilot_argv(spec: LaunchSpec) -> list[str]:
    """Author only (K1): the pinned tool set, write approval for the return file, for a bound author the launcher's
    bare-name shell rule plus a stdin-only sha256sum (never `uv`), write deny rules for the protected paths, and
    RUN/worker-return as the only added directory. No reviewer or isolated argv exists."""
    if spec.isolated or spec.mode != "workspace-write" or not spec.return_path:
        raise DomainError("UNSUPPORTED_CLIENT_MODE", "Worker client copilot has no write-free read-only mode")
    rules = [f"shell({LAUNCHER_NAME})", "shell(sha256sum)"] if spec.commands else []
    argv = [spec.binary, "-p", ENVELOPE_ARG, "--output-format", "json", "--session-id", spec.session_id,
            "--model", spec.model, *COPILOT_ISOLATION, "--log-dir", str(spec.run_dir / "copilot-logs"),
            "-C", str(spec.worktree), "--add-dir", str(spec.run_dir / WORKER_RETURN),
            "--available-tools", ",".join(sorted(COPILOT_TOOLS)), "--allow-tool", "write"]
    for rule in rules:
        argv += ["--allow-tool", rule]
    for rule in COPILOT_DENIED:
        argv += ["--deny-tool", rule]
    return argv


def _home_session(session: object) -> bool:
    """Whether $HOME/.copilot/session-state/<id> exists: a session that leaked into the shared home."""
    return isinstance(session, str) and bool(session) and \
        os.path.lexists(Path(os.path.expanduser("~")) / ".copilot" / "session-state" / session)


def _copilot_launch_facts(spec: LaunchSpec) -> dict:
    """Pre-spawn facts. `isolation.gaps` names the worktree skill trees Copilot loads as project skills (P0c), and a
    bound author's `script_launcher` seals the launcher bytes by digest (they derive from the bound commands)."""
    removed, present = removed_env(COPILOT), static_env(COPILOT)
    inherited = sorted(name for name in os.environ if name.startswith(COPILOT_INHERITED) and name not in removed)
    facts = {"requested_mode": spec.mode, "confinement": COPILOT.confinement,
             "isolation": {"flags": COPILOT_ISOLATION, "deny_rules": COPILOT_DENIED, "env": dict(COPILOT.env),
                           "inherited_names": inherited, "removed_names": removed, "kept_names": kept_env(COPILOT),
                           "token_names": [name for name in COPILOT_TOKENS if name in present],
                           "gaps": [f"project-skills:{item}" for item in COPILOT_SKILL_DIRS
                                    if (spec.worktree / item).is_dir()]},
             "session_id": spec.session_id, "return_path": spec.return_path,
             "pre_spawn_at": datetime.now(timezone.utc).isoformat(),
             "home_session_preexisted": _home_session(spec.session_id)}
    if spec.commands:  # the runner wrote these bytes; launcher-check.json rechecks them after the run
        tools = launcher_tools()
        facts["script_launcher"] = {
            "path": str(launcher_path(spec.run_dir)), "rule": f"shell({LAUNCHER_NAME})",
            "path_prepend": str(launcher_path(spec.run_dir).parent), "python": tools["python"], "uv": tools["uv"],
            "sha256": hashlib.sha256(launcher_bytes(spec.commands, tools, str(spec.worktree))).hexdigest()}
    return facts


def _copilot_run_env(spec: LaunchSpec) -> dict[str, str]:
    """The per-run COPILOT_HOME and, for a bound author, RUN/worker-tools first on PATH, so the launcher answers to its
    bare name (ruling R3a); the inherited PATH follows, and only the prepended directory is recorded."""
    env = {"COPILOT_HOME": str(spec.run_dir / "copilot-home")}
    if spec.commands:
        env["PATH"] = str(launcher_path(spec.run_dir).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
    return env


def _open_nofollow(base: Path, *parts: str) -> int:
    """A descriptor for base/parts with no symlink at any step; the last part may be a file."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            step = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if last else os.O_DIRECTORY), dir_fd=fd)
            os.close(fd)
            fd = step
        return fd
    except BaseException:
        os.close(fd)
        raise


def _yaml_scalar(text: str, key: str) -> str | None:
    """One top-level `key: value` line of workspace.yaml (optionally quoted); None when absent or repeated."""
    values = [line[len(key) + 1:].strip() for line in text.splitlines() if line.startswith(key + ":")]
    if len(values) != 1:
        return None
    value = values[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value or None


def _copilot_workspace(run_dir: Path, session: str) -> dict | None:
    try:
        fd = _open_nofollow(run_dir / "copilot-home", "session-state", session, "workspace.yaml")
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(MAX_WORKSPACE_BYTES + 1)
        if len(data) > MAX_WORKSPACE_BYTES:
            return None
        text = data.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    return {"sha256": hashlib.sha256(data).hexdigest(), "cwd": _yaml_scalar(text, "cwd"),
            "created_at": _yaml_scalar(text, "created_at")}


def _listing(base: Path, *parts: str) -> list[str] | None:
    try:
        fd = _open_nofollow(base, *parts)
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)
    except OSError:
        return None


def _copilot_observe(run_dir: Path, launch: dict) -> dict:
    """Once, after the group drain: the live session folders of the per-run home (plus the listing of the lock folder
    Copilot creates beside them, when present) and the $HOME leak check, sealed by the runner as freshness.json;
    replay reads only that seal."""
    session = launch.get("session_id")
    sessions = _listing(run_dir / "copilot-home", "session-state")
    workspace = _copilot_workspace(run_dir, session) if isinstance(session, str) and session in (sessions or ()) \
        else None
    value = {"sessions": sessions, "workspace": workspace, "home_session_after": _home_session(session)}
    if COPILOT_LOCKS in (sessions or ()):
        value["locks"] = _listing(run_dir / "copilot-home", "session-state", COPILOT_LOCKS)
    return value


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp is missing")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("timestamp has no time zone")
    return instant


def _copilot_fresh(run_dir: Path, launch: dict, session: str) -> None:
    """Design section 14 freshness from the sealed observation and the launch receipt only."""
    try:
        fresh = json.loads((run_dir / "freshness.json").read_bytes())
    except (ValueError, UnicodeError) as exc:  # never blamed on the stream (return-write fix 1, F4)
        raise ValueError("the sealed freshness.json is not one JSON object") from exc
    if not isinstance(fresh, dict):
        raise ValueError("the sealed freshness.json is not one JSON object")
    workspace = fresh.get("workspace")
    locked = isinstance(workspace, dict) and "locks" in fresh  # R1: only the lock folder holding only <id>.lock
    if locked and fresh["locks"] != [session + ".lock"]:
        raise ValueError("the lock folder holds more or other than the session's lock")
    expected = sorted([COPILOT_LOCKS, session]) if locked else [session]
    if not isinstance(workspace, dict) or fresh.get("sessions") != expected or \
       workspace.get("cwd") != launch.get("cwd", launch.get("worktree")) or \
       fresh.get("home_session_after") is not False or launch.get("home_session_preexisted") is not False:
        raise ValueError("the session is not the one fresh session of the per-run home")
    start = _instant(launch.get("pre_spawn_at"))
    if _instant(workspace.get("created_at")) < start.replace(microsecond=start.microsecond // 1000 * 1000):
        raise ValueError("the session was created before the spawn")


def _strings(value: object) -> list[str]:
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [item for item in items if isinstance(item, str)]


def _offered(data: dict) -> set[str]:
    """Tool names a `session.usage_checkpoint` record says the model was offered (P0c shape:
    promptCacheBreakState[].models.<model>.tools[].name); malformed parts carry no names."""
    names = set()
    states = data.get("promptCacheBreakState")
    for state in states if isinstance(states, list) else ():
        models = state.get("models") if isinstance(state, dict) else None
        for model in models.values() if isinstance(models, dict) else ():
            tools = model.get("tools") if isinstance(model, dict) else None
            names |= {item["name"] for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)} \
                if isinstance(tools, list) else set()
    return names


def _writes_path(command: object, path: str) -> bool:
    """Whether a shell command text writes exactly `path`: the target of `>`, `>>`, `>|`, `&>` or `tee [-a]`, optionally
    quoted, ending on a shell-word terminator (return-write fix 1, F2/F3). A read, `cp`/`mv`, a `$VAR` path, a mention
    in a heredoc body or a comment fails closed."""
    return isinstance(command, str) and re.search(
        r"""(?:>{1,2}\|?|\btee(?:\s+-a)?)\s*["']?""" + re.escape(path) + r"""["']?(?=$|[\s;|&)<>])""",
        command) is not None


def _cause(exc: BaseException) -> str:
    """The specific reason a Copilot terminal was refused, for the run's evidence needs (return-write round)."""
    if isinstance(exc, (json.JSONDecodeError, UnicodeError)) or str(exc) == "invalid JSONL event":
        return "the stream holds a line that is not one JSON object with a string type"
    if isinstance(exc, OSError):
        return "a retained run file (events.jsonl or freshness.json) is missing or unreadable"
    if type(exc) is ValueError:
        return str(exc)
    return "a stream record or the sealed freshness has an unexpected shape"


def _gap(prefix: str, value: object, data: dict) -> str:
    return prefix + (value if isinstance(value, str) and value else canonical(data).decode())


def _copilot_terminal(run_dir: Path, launch: dict) -> Terminal:
    """Design section 14 over the retained stream, the sealed freshness.json and the launch receipt: every line one
    JSON object with a string type; exactly one `result`, last, with exitCode 0 and the preset sessionId; every
    reported session id the preset; no session.error; one fresh session. Every tool.execution_start must name its tool
    (only start records carry toolName), and when any is present the return write must show: a start of a write tool
    naming the return path (basis "tool") or a `bash` start whose command writes exactly that path through `>`, `>>`,
    `>|`, `&>` or `tee` (basis "shell"; smoke 3 saw an approved heredoc redirect), never a start whose own completion
    was denied; recorded as return_write_basis. The names feed tool_check; none present leaves
    tool_check blocked. A tool.execution_complete whose error.code is
    "denied" is a denial (P0c: the only denial record in stdout), named by its start's toolCallId; a denied
    permission.completed, when present, still counts. Tools a session.usage_checkpoint says were offered must lie in
    the pinned author set (recorded as offered_tools). MCP servers (except those reported disabled), hooks and
    subagents are isolation gaps. The final bytes come only from the return file, never from an assistant message. A
    refusal names its specific cause."""
    try:
        seen = jsonl_records(run_dir / "events.jsonl")
        session, return_path = launch.get("session_id"), launch.get("return_path")
        if not isinstance(session, str) or not isinstance(return_path, str):
            raise ValueError("launch receipt lacks the preset session or the return path")
        if [index for index, item in enumerate(seen) if item["type"] == "result"] != [len(seen) - 1]:
            raise ValueError("stream lacks exactly one result as its last record")
        result = seen[-1]
        if result.get("sessionId") != session:
            raise ValueError("the result record names another session than the preset")
        if type(result.get("exitCode")) is not int or result["exitCode"] != 0:
            raise ValueError("the result record's exitCode is not 0")
        names, writes, denials, gaps, version, calls, offered = [], [], [], [], None, {}, set()
        for item in seen:
            kind, data = item["type"], item.get("data") if isinstance(item.get("data"), dict) else {}
            if any(value is not None and value != session for value in (item.get("sessionId"), data.get("sessionId"))):
                raise ValueError("stream session differs from the preset")
            if kind == "session.error":
                raise ValueError("stream reports a session error")
            if kind == "session.start" and version is None:
                version = data.get("copilotVersion")
            if kind == "tool.execution_start":
                name = data.get("toolName")
                if type(name) is not str or not name:
                    raise ValueError("tool event without a tool name")
                names.append(name)
                arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
                if name in COPILOT_WRITE_TOOLS and return_path in _strings(arguments):
                    writes.append((data.get("toolCallId"), "tool"))
                elif name == "bash" and _writes_path(arguments.get("command"), return_path):
                    writes.append((data.get("toolCallId"), "shell"))
                if isinstance(data.get("toolCallId"), str):
                    calls[data["toolCallId"]] = name
            elif kind == "tool.execution_complete":
                error = data.get("error")
                if isinstance(error, dict) and error.get("code") == "denied":
                    call = data.get("toolCallId") if isinstance(data.get("toolCallId"), str) else None
                    denials.append({"toolCallId": call, "toolName": calls.get(call), "error": error})
            elif kind == "session.usage_checkpoint":
                offered |= _offered(data)
            elif kind == "permission.completed":
                outcome = data.get("result")
                if not isinstance(outcome, dict) or type(outcome.get("kind")) is not str:
                    raise ValueError("permission outcome without a kind")
                if outcome["kind"].startswith("denied"):
                    denials.append(data)
            elif kind == "session.mcp_servers_loaded" and isinstance(data.get("servers"), list):
                gaps += [_gap("mcp-server:", item.get("name") if isinstance(item, dict) else None,
                              item if isinstance(item, dict) else {"server": item}) for item in data["servers"]
                         if not (isinstance(item, dict) and item.get("status") == "disabled")]  # P0c: not loaded
            elif kind.startswith("session.mcp_"):
                gaps.append(_gap("mcp-server:", data.get("serverName"), data))
            elif kind.startswith("hook."):
                gaps.append(_gap("hook:", data.get("hookType"), data))
            elif kind.startswith("subagent."):
                gaps.append(_gap("subagent:", data.get("agentName"), data))
        denied = {item.get("toolCallId") for item in denials if isinstance(item.get("toolCallId"), str)}
        bases = {basis for call, basis in writes if isinstance(call, str) and call not in denied}  # F1: ran only;
        # a start without a toolCallId cannot be matched to a denial, so it never counts (fail closed)
        if names and not bases:
            raise ValueError("tool events name no write of the return path (no create or edit naming it and no bash "
                             "redirect or tee writing it, among the calls not denied)")
        if offered - COPILOT_TOOLS:  # fix round 2 (M3): the pin did not hold
            raise ValueError("the model was offered a tool outside the pinned author set")
        _copilot_fresh(run_dir, launch, session)
        facts = {"usage": result.get("usage"), "copilotVersion": version, "denials": denials,
                 "isolation_gaps": sorted(set(gaps))}
        if offered:
            facts["offered_tools"] = sorted(offered)
        if names:  # the names tool_check reads; no tool events is no tool information
            facts["tools"] = sorted(set(names))
            facts["return_write_basis"] = "tool" if "tool" in bases else "shell"
        return Terminal(session, read_worker_return(run_dir), facts)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise _unknown("Copilot terminal refused: " + _cause(exc)) from exc


COPILOT = Client(
    name="copilot", executable="copilot", confinement="approval-policy-advisory", presets_session=True,
    return_source="worker-file", envelope_via="argv", max_envelope_bytes=100_000, write_free_readonly=False,
    max_turns_default=None, env={"COPILOT_AUTO_UPDATE": "false"},
    # a parent Copilot session's COPILOT* variables (allow-all, instruction dirs, model, telemetry) never reach the
    # worker; the documented token COPILOT_GITHUB_TOKEN is kept (env_keep); names recorded, never values
    env_unset=frozenset({"COPILOT*", "COPILOT_ALLOW_ALL"}), env_keep=frozenset({"COPILOT_GITHUB_TOKEN"}),
    session_scrub=PARENT_MARKERS,
    run_subdirs=("copilot-home", "copilot-logs", WORKER_RETURN), private_subdirs=("copilot-home", "copilot-logs"),
    observation_name="freshness.json", internal_tools=COPILOT_TOOLS,  # narrowed P0c vocabulary (R2a), reviewed
    run_env=_copilot_run_env,
    allowed_tools=lambda mode: COPILOT.internal_tools if mode == "workspace-write" else frozenset(),
    argv=_copilot_argv, launch_facts=_copilot_launch_facts, observe=_copilot_observe, terminal=_copilot_terminal,
    script_launcher=True)
REGISTRY: dict[str, Client] = {CODEX.name: CODEX, CLAUDE.name: CLAUDE, COPILOT.name: COPILOT}  # closed; tests may
# inject entries, production never


def get(name: str | None) -> Client:
    name = DEFAULT if name is None else name
    client = REGISTRY.get(name) if isinstance(name, str) else None
    if client is None or client.name != name:
        raise DomainError("INVALID_INPUT", "Unknown worker client")
    return client


def bound_client(binding: dict) -> Client:
    """The client sealed in a binding (absent: the default). A malformed or unknown sealed value is refused,
    never read as unbound."""
    name = binding.get("client", DEFAULT)
    if type(name) is not str:
        raise DomainError("INVALID_INPUT", "Binding names a malformed worker client")
    return get(name)


def source_id(name: str, thread: str) -> str:
    return f"{name}:{thread}"


def client_of(source: str) -> str | None:
    """The registered client named by a source id prefix, else None."""
    if not isinstance(source, str):
        return None
    name, separator, rest = source.partition(":")
    return name if separator and rest and name in REGISTRY else None


def _scrub_prefixes(client: Client) -> tuple[str, ...]:
    return tuple(item[:-1] for item in client.env_unset | client.session_scrub if item.endswith("*"))


def removed_env(client: Client) -> list[str]:
    """Inherited names env_unset and session_scrub remove: exact names, and every name with a `PREFIX*` entry's
    prefix except the client's env_keep. Recorded by name only, never by value."""
    prefixes, exact = _scrub_prefixes(client), client.env_unset | client.session_scrub
    return sorted(name for name in os.environ if name in exact or
                  (prefixes and name.startswith(prefixes) and name not in client.env_keep))


def kept_env(client: Client) -> list[str]:
    """Inherited names a `PREFIX*` scrub keeps because the client's env_keep lists them; names only."""
    prefixes, exact = _scrub_prefixes(client), client.env_unset | client.session_scrub
    return sorted(name for name in os.environ if prefixes and name.startswith(prefixes) and
                  name in client.env_keep and name not in exact)


def scrub_record(client: Client) -> dict | None:
    """The env-scrub.json sidecar of a client with session_scrub: the patterns and the names removed and kept, never
    values. A sidecar, so a Codex launch.json keeps its bytes (design D4)."""
    if not client.session_scrub:
        return None
    return {"patterns": sorted(client.env_unset | client.session_scrub), "removed_names": removed_env(client),
            "kept_names": kept_env(client)}


def static_env(client: Client) -> dict[str, str]:
    """Inherited env minus env_unset plus the static env: the preflight env and the base of every spawn env."""
    removed = set(removed_env(client))
    return {**{key: value for key, value in os.environ.items() if key not in removed}, **client.env}


def preflight(client: Client) -> tuple[str, str]:
    """D5: absolute executable path and its --version text, before any reservation; never a model call."""
    env = static_env(client)
    found = shutil.which(client.executable, path=env.get("PATH", os.defpath))
    if not found:
        raise DomainError("BLOCKED_EVIDENCE", f"Worker client executable {client.executable} is not on PATH")
    binary = os.path.abspath(found)
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3, env=env,
                                stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise DomainError("BLOCKED_EVIDENCE", f"Worker client {client.name} version probe failed") from exc
    version = (result.stdout.strip().splitlines() or [""])[0].strip()  # the first line (Copilot adds an update hint)
    if result.returncode != 0 or not version:
        raise DomainError("BLOCKED_EVIDENCE", f"Worker client {client.name} version probe failed")
    return binary, version


def doctor_notes(client: Client) -> dict:
    """Doctor-only additions: for Copilot, the documented token names present (names only) and a warning when
    COPILOT_GITHUB_TOKEN is absent; nothing for other clients, whose doctor output is unchanged."""
    if client.name != COPILOT.name:
        return {}
    env = static_env(client)
    return {"token_names": [name for name in COPILOT_TOKENS if name in env],
            "warnings": [] if "COPILOT_GITHUB_TOKEN" in env else [COPILOT_TOKEN_WARNING]}


def tool_check(client: Client, mode: str, facts: dict, web: bool = False) -> str:
    """T5: no declared check is not-checked; a declared check without tool information is blocked. A web writer
    (design D23, `web` from sealed evidence) may also report the two web tools."""
    if client.allowed_tools is None:
        return "not-checked"
    tools = facts.get("tools")
    if tools is None:
        return "blocked"
    allowed = client.allowed_tools(mode) | (frozenset(WEB_TOOLS.split(",")) if web and mode != "read-only"
                                            else frozenset())
    if not isinstance(tools, list) or any(type(item) is not str for item in tools) or not set(tools) <= allowed:
        raise _unknown("Worker reported a tool outside the allowed tools")
    return "passed"


STOPPED = "the client stopped at its turn limit without a return"   # design D25: an isolated run's stop


def exited_terminal(client: Client, run_dir: Path, launch: dict) -> Terminal:
    """Design D25: the terminal of a run that exited non-zero. Only a client stop outlives the exit code; any other
    stream (none, malformed or a return) leaves the exit code the cause, as before the stream was read first."""
    try:
        terminal = client.terminal(run_dir, launch)
    except (OSError, ValueError, DomainError) as exc:
        raise _unknown("Worker process exited unsuccessfully") from exc
    if terminal.stop is None:
        raise _unknown("Worker process exited unsuccessfully")
    return terminal


def client_facts(client: Client, mode: str, terminal: Terminal, web: bool = False) -> dict:
    """Recorded client facts; empty (and so absent from the receipt) for a client without facts or tool check."""
    check = tool_check(client, mode, terminal.facts, web)
    facts = dict(terminal.facts)
    if facts or check != "not-checked":
        facts["tool_check"] = check
    return facts


def recorded_tool_check(receipt: dict) -> str:
    return receipt.get("client_facts", {}).get("tool_check", "not-checked")


def launch_env(client: Client, run_env: dict[str, str]) -> dict:
    """Names only (T4); empty for a client without static, unset or per-run variables."""
    names = sorted(set(client.env) | set(run_env))
    return {"env_names": names, "env_unset": sorted(client.env_unset)} if names or client.env_unset else {}


def spawn_argv(client: Client, argv: list[str], envelope: bytes) -> list[str]:
    if client.envelope_via != "argv":
        return argv
    return [envelope.decode("utf-8") if item == ENVELOPE_ARG else item for item in argv]


def _worker_return_raw(run_dir: Path) -> bytes:
    fd = os.open(run_dir / WORKER_RETURN, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if sorted(os.listdir(fd)) != ["return.json"]:
            raise ValueError("worker-return must hold exactly return.json")
        file_fd = os.open("return.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_RETURN_BYTES:
                raise ValueError("worker return is not one private-sized regular file")
            data = stream.read(MAX_RETURN_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RETURN_BYTES:
        raise ValueError("worker return is too large")
    return data


def _check_depth(text: str) -> None:
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("worker return nests too deeply")
        elif char in "]}":
            depth -= 1


def read_worker_return(run_dir: Path) -> bytes:
    """T1 transport shape only: RUN/worker-return holds exactly one regular, single-link, at most 1 MB return.json
    of one JSON object nested at most 64 deep; returns canonical bytes. Field checks stay in _final and profiles."""
    try:
        text = _worker_return_raw(Path(run_dir)).decode("utf-8")
        _check_depth(text)
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("worker return is not one JSON object")
        return canonical(value)
    except (RecursionError, ValueError, UnicodeError, OSError) as exc:
        raise _unknown("Worker return file is missing or malformed") from exc


def worker_return_sha256(run_dir: Path) -> str:
    try:
        return hashlib.sha256(_worker_return_raw(Path(run_dir))).hexdigest()
    except (ValueError, OSError) as exc:
        raise _unknown("Worker return file is missing or malformed") from exc


def _signal_group(pgid: int, sig: signal.Signals) -> str:
    """One group signal; never raises, so a denied or failed signal cannot skip the run's end markers (M1)."""
    try:
        os.killpg(pgid, sig)
        return sig.name
    except ProcessLookupError:
        return sig.name + ":missing"
    except PermissionError:
        return sig.name + ":denied"
    except OSError as exc:
        return f"{sig.name}:error:{exc.errno}"


def _signal_failed(outcome: str) -> bool:
    return outcome.endswith(":denied") or ":error:" in outcome


def stop_group(process: subprocess.Popen) -> list[str]:
    """Attempt bounded group termination; never imply descendant quiescence."""
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    attempts = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        attempts.append(_signal_group(process.pid, sig))
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:  # the group signals failed; the leader is our own child, so stop and reap it
        process.kill()
        attempts.append("SIGKILL:leader")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return attempts


def own_group(process: subprocess.Popen) -> None:
    """Every spawn uses start_new_session=True, so the group is exclusively the run's; checked before any killpg."""
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None
    if pgid != process.pid:
        process.kill()
        raise _unknown("Worker process is not its own process group leader")


def _group_alive(process: subprocess.Popen) -> bool:
    process.poll()  # reap the leader so only real members keep the group alive
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: a member exists that the runner may not signal
        return True
    return True


def _group_gone(process: subprocess.Popen, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _group_alive(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
    return True


def drain_group(process: subprocess.Popen | None, earlier: list[str] = ()) -> dict:
    """N2: after the leader ends, wait for the group, then TERM, wait, KILL, poll; record what happened. A denied
    or failed signal here or in `earlier` (stop_group) is recorded as group_signal_error and is never terminated."""
    if process is None:
        return {"group_alive_after_exit": False, "group_signals": [], "group_terminated": True}
    alive = _group_alive(process)
    gone = not alive or _group_gone(process, GROUP_WAIT)
    signals = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if gone:
            break
        signals.append(_signal_group(process.pid, sig))
        gone = _group_gone(process, GROUP_WAIT)
    result = {"group_alive_after_exit": alive, "group_signals": signals, "group_terminated": gone}
    errors = [item for item in [*earlier, *signals] if _signal_failed(item)]
    if errors:
        result.update(group_signal_error=errors, group_terminated=False)
    return result


def _proc_start_time(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def launch_intent(pid: int, run_dir: Path, session_id: str | None) -> dict:
    """Driver I2: the first post-spawn write; it carries the pid, so it cannot exist before the spawn."""
    value = {"pid": pid, "pgid": pid, "start_time": _proc_start_time(pid), "boot_id": _boot_id(),
             "run_dir": str(run_dir), "observed_at": datetime.now(timezone.utc).isoformat()}
    if session_id is not None:
        value["session_id"] = session_id
    return value


def _digest(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            return hashlib.sha256(stream.read()).hexdigest()
    except OSError:
        return None


def _walk(top: Path, skip: tuple[str, ...] = ()):
    """Regular-file candidates below top; no dot-prefixed component, no symlinked directory, top-level skips."""
    for here, dirs, names in os.walk(top):
        first = Path(here) == top
        dirs[:] = sorted(item for item in dirs if not item.startswith(".") and not (first and item in skip))
        for name in sorted(names):
            if not name.startswith("."):
                yield Path(here) / name


def private_subdirs_of(run: Path) -> tuple[str, ...]:
    """From the run's recorded client; no launch.json, a legacy run or an unregistered client excludes nothing.
    Never raises; `worker-return` is never private."""
    try:
        fd = os.open(run / "launch.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return ()
            value = json.loads(stream.read(MAX_RETURN_BYTES))
        name = value.get("client") if isinstance(value, dict) else None
        client = REGISTRY.get(name) if isinstance(name, str) else None
        if client is None or client.name != name:
            return ()
        return tuple(item for item in client.private_subdirs if isinstance(item, str) and item != WORKER_RETURN)
    except Exception:  # noqa: BLE001 - the lookup is advisory and must never raise
        return ()


def sealed_evidence(repo: RepoIdentity, own_run: str) -> dict[str, str]:
    """D16 baseline: digests of the sealed files already present under <common-dir>/coordinator."""
    base = repo.common_dir / "coordinator"
    found = {}

    def add(path: Path) -> None:
        digest = _digest(path)
        if digest is not None:
            found[path.relative_to(base).as_posix()] = digest

    for top in SEALED_TOPS:
        for path in _walk(base / top):
            add(path)
    runs = base / "runs"
    try:
        names = sorted(os.listdir(runs)) if not runs.is_symlink() else []
    except OSError:
        names = []
    for name in names:
        run = runs / name
        if name == own_run or name.startswith(".") or run.is_symlink() or not run.is_dir():
            continue
        if any(os.path.lexists(run / item) for item in ENDED):
            for path in _walk(run, private_subdirs_of(run)):
                add(path)
        else:
            for item in RUNNING_SEALED:
                add(run / item)
    return found


def verify_sealed(repo: RepoIdentity, baseline: dict[str, str]) -> tuple[list[str], list[str]]:
    """Re-digest the baseline: (modified, deleted). Additions are out of scope."""
    base = repo.common_dir / "coordinator"
    modified, deleted = [], []
    for relative, digest in sorted(baseline.items()):
        path = base / relative
        if not os.path.lexists(path):
            deleted.append(relative)
        elif _digest(path) != digest:
            modified.append(relative)
    return modified, deleted


def integrity_record(repo: RepoIdentity, baseline: dict[str, str], group: dict) -> dict:
    modified, deleted = verify_sealed(repo, baseline)
    return {"checked": len(baseline), "modified": modified, "deleted": deleted, **group}


def integrity_clean(record: object) -> bool:
    return isinstance(record, dict) and record.get("modified") == [] and record.get("deleted") == [] and \
        record.get("group_terminated") is True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_lines(path: Path, *args: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("git failed")
    return result.stdout.splitlines()


def _exact(path: Path, label: str) -> Path:
    """Absolute, not a symlink and free of symlinked components."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or Path(os.path.realpath(path)) != path:
        raise DomainError("UNSAFE_PATH", f"Isolated run {label} must be an absolute path without symlinks")
    return path


def _isolated_paths(target: RepoIdentity, root: Path, cwd: Path, receipt_dir: Path) -> None:
    root, cwd = _exact(root, "root"), _exact(cwd, "cwd")
    receipt_dir = Path(receipt_dir)
    if os.path.lexists(receipt_dir):
        raise DomainError("INVALID_INPUT", "Isolated run receipt directory already exists")
    _exact(receipt_dir.parent, "receipt directory parent")
    if not receipt_dir.is_absolute() or receipt_dir.name in ("", ".", ".."):
        raise DomainError("UNSAFE_PATH", "Isolated run receipt directory must be an absolute path")
    if not root.is_dir() or not cwd.is_dir():
        raise DomainError("INVALID_INPUT", "Isolated run root and cwd must be existing directories")
    if cwd == root or not cwd.is_relative_to(root) or receipt_dir == root or not receipt_dir.is_relative_to(root):
        raise DomainError("UNSAFE_PATH", "Isolated run cwd and receipt directory must lie under the root")
    if receipt_dir.is_relative_to(cwd):
        raise DomainError("UNSAFE_PATH", "Isolated run receipt directory must not lie inside the cwd")
    try:
        protected = [Path(os.path.realpath(line[len("worktree "):]))
                     for line in _git_lines(target.worktree, "worktree", "list", "--porcelain")
                     if line.startswith("worktree ")]
    except (OSError, ValueError) as exc:
        raise DomainError("INVALID_REPOSITORY", "Cannot list the target worktrees") from exc
    protected += [Path(os.path.realpath(target.common_dir)), Path(os.path.realpath(target.worktree))]
    if any(path.is_relative_to(item) for path in (root, cwd, receipt_dir) for item in protected):
        raise DomainError("UNSAFE_PATH", "Isolated run paths must lie outside the target worktrees and common dir")
    try:
        common, top = _git_lines(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel")
    except (OSError, ValueError) as exc:
        raise DomainError("INVALID_INPUT", "Isolated run cwd must be its own Git repository") from exc
    if Path(os.path.realpath(top)) != cwd or Path(os.path.realpath(common)) != cwd / ".git":
        raise DomainError("INVALID_INPUT", "Isolated run cwd must be its own Git repository")


def _schema_keys(final: object, schema: dict) -> dict:
    if not isinstance(final, dict) or set(schema.get("required", ())) - set(final) or \
       (schema.get("additionalProperties") is False and set(final) - set(schema.get("properties", {}))):
        raise _unknown("Isolated final message does not match the schema keys")
    return final


def run_isolated(client: str, target: RepoIdentity, root: Path, cwd: Path, receipt_dir: Path, envelope: dict,
                 schema: dict, model: str, timeout_seconds: int, max_turns: int | None = None) -> IsolatedRun:
    """D17: one read-only process outside any project; no assignment, fence or DB anchor. Receipts stay in
    receipt_dir; the caller copies and anchors them. `observed` means well-formed sealed transport only."""
    chosen = get(client)
    if not chosen.write_free_readonly:
        raise DomainError("UNSUPPORTED_CLIENT_MODE", f"Worker client {chosen.name} has no write-free read-only mode")
    if type(model) is not str or not model.strip() or model.startswith("-") or type(timeout_seconds) is not int or \
       timeout_seconds <= 0 or not isinstance(envelope, dict) or not isinstance(schema, dict):
        raise DomainError("INVALID_INPUT", "Isolated run needs an envelope, a schema, a model and a positive timeout")
    if max_turns is not None and (chosen.max_turns_default is None or type(max_turns) is not int or max_turns <= 0):
        raise DomainError("INVALID_INPUT", "Max turns is not supported for this client or is not positive")
    receipt_dir = Path(receipt_dir)
    _isolated_paths(target, Path(root), Path(cwd), receipt_dir)
    data = canonical(envelope)
    if chosen.max_envelope_bytes is not None and len(data) > chosen.max_envelope_bytes:
        raise DomainError("INVALID_INPUT", "Envelope exceeds the client's envelope limit")
    binary, version = preflight(chosen)
    try:
        receipt_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DomainError("INVALID_INPUT", "Isolated run receipt directory already exists") from exc
    except OSError as exc:
        raise DomainError("UNSAFE_PATH", "Cannot create the isolated run receipt directory") from exc
    for name in chosen.run_subdirs:
        (receipt_dir / name).mkdir(mode=0o700)
    cwd = Path(cwd)
    before = inventory(cwd)
    private_file(receipt_dir / "envelope.json", data)
    private_file(receipt_dir / "return.schema.json", canonical(schema))
    if chosen.return_source == "client-file":
        private_file(receipt_dir / "last.json", b"")
    session = str(uuid.uuid4()) if chosen.presets_session else None
    spec = LaunchSpec(binary, "read-only", cwd, receipt_dir, schema, model,
                      max_turns if max_turns is not None else chosen.max_turns_default, session, (), (),
                      isolated=True)
    argv = chosen.argv(spec)
    run_env = chosen.run_env(spec)
    facts = chosen.launch_facts(spec)  # taken before the spawn, as in worker.run_assignment
    scrub = scrub_record(chosen)
    launch_digest = None

    def unknown(*needs: str) -> IsolatedRun:
        return IsolatedRun("unknown", None, None, str(receipt_dir), launch_digest, None, list(needs))

    out_fd = os.open(receipt_dir / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    err_fd = os.open(receipt_dir / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(out_fd, "wb") as output, os.fdopen(err_fd, "wb") as error:
        process = None
        try:
            process = subprocess.Popen(spawn_argv(chosen, argv, data), cwd=cwd, stdin=subprocess.PIPE,
                                       stdout=output, stderr=error, start_new_session=True, umask=0o077,
                                       env={**static_env(chosen), **run_env})
            own_group(process)
            seal(receipt_dir / "launch-intent.json", launch_intent(process.pid, receipt_dir, session))
            launch = {"kind": "isolated-launch", "client": chosen.name, "binary": binary, "argv": argv,
                      "source_id": source_id(chosen.name, "process"), "pid": str(process.pid), "pgid": process.pid,
                      "cwd": str(cwd), "root": str(root), "target_common_dir": str(target.common_dir),
                      "requested_mode": "read-only", "confinement": chosen.confinement, "model": model,
                      "cli_version": version, "envelope_sha256": hashlib.sha256(data).hexdigest(),
                      "observed_at": _now(), **launch_env(chosen, run_env), **facts}
            if session is not None:
                launch["session_id"] = session
            if spec.max_turns is not None:
                launch["max_turns"] = spec.max_turns
            launch_digest = seal(receipt_dir / "launch.json", launch)
            if scrub is not None:
                seal(receipt_dir / "env-scrub.json", scrub)
            try:
                process.communicate(input=data if chosen.envelope_via == "stdin" else b"", timeout=timeout_seconds)
                timed_out, signals = False, []
            except subprocess.TimeoutExpired:
                timed_out, signals = True, stop_group(process)
            group = drain_group(process, signals)
            if timed_out:
                private_file(receipt_dir / "timeout.json", canonical({"signals": signals, "pid": process.pid,
                                                                     "exit_code": process.poll(), "ended_at": _now()}))
            seal(receipt_dir / "end.json", {"pid": process.pid, "pgid": process.pid, "exit_code": process.poll(),
                                            "ended_at": _now(), "timed_out": timed_out, **group})
        except BaseException as exc:
            signals = stop_group(process) if process is not None else []
            group = drain_group(process, signals)
            try:
                private_file(receipt_dir / "interrupted.json", canonical({
                    "pid": process.pid if process else None, "signals": signals,
                    "exit_code": process.poll() if process else None, "ended_at": _now(), **group}))
            except OSError:
                pass
            if isinstance(exc, (OSError, DomainError, subprocess.SubprocessError)):
                return unknown("launch or process evidence interrupted")
            raise
    if timed_out:
        return unknown("timeout; the process group was signalled")
    if not group["group_terminated"]:
        return unknown("process group survived termination")
    try:
        if process.returncode != 0:   # design D25: the stream is read first, so a stop is named by its cause
            exited_terminal(chosen, receipt_dir, launch)
            raise _unknown(STOPPED)
        after = inventory(cwd)
        if delta(before, after):
            raise _unknown("Isolated run changed its cwd")
        extras = {}
        if chosen.observe is not None:
            extras["observation_sha256"] = seal(receipt_dir / chosen.observation_name,
                                                chosen.observe(receipt_dir, launch))
        terminal = chosen.terminal(receipt_dir, launch)
        if terminal.stop is not None:   # design D25: an isolated run saves nothing; a stop is unknown
            raise _unknown(STOPPED)
        if chosen.return_source == "stdout":
            private_file(receipt_dir / "last.json", terminal.final)
        elif chosen.return_source != "client-file":
            raise _unknown("Isolated runs take client-file or stdout returns only")
        last = (receipt_dir / "last.json").read_bytes()
        if last != terminal.final:
            raise _unknown("Final message differs from the client terminal")
        final = _schema_keys(json.loads(last), schema)
        facts = client_facts(chosen, "read-only", terminal)
        if facts:
            extras["client_facts"] = facts
        source = source_id(chosen.name, terminal.thread_id)
        return_digest = seal(receipt_dir / "return.json", {
            "kind": "isolated-return", "client": chosen.name, "source_id": source, "thread_id": terminal.thread_id,
            "exit_code": process.returncode, "launch_sha256": launch_digest,
            "envelope_sha256": hashlib.sha256(data).hexdigest(),
            "events_sha256": hashlib.sha256((receipt_dir / "events.jsonl").read_bytes()).hexdigest(),
            "last_sha256": hashlib.sha256(last).hexdigest(), "before": stamp_json(before), "after": stamp_json(after),
            "ended_at": _now(), **extras})
        return IsolatedRun("observed", source, final, str(receipt_dir), launch_digest, return_digest, [])
    except (OSError, ValueError, DomainError) as exc:
        return unknown(str(exc))
