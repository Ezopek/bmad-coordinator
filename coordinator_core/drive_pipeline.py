"""Persisted drive pipeline (unattended-driver design D6, D7 and section 5): closed-kind validation, a sealed
content-addressed file anchored by an owner-fenced `pipeline.bound` event, and a pure derivation of every step's state
from existing rows and receipts. No step state is ever stored."""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import bmad_contract as contract
from . import bmad_implementation as implementation
from . import bmad_planning, bmad_spec_author, bundles, clients, judgment
from .bmad_review import CODE_LENSES, SPEC_LENSES, finding_counts
from .model import DecisionInput, DomainError, OwnerRef
from .run_receipt import canonical, private_file, read_binding, read_receipt
from .service import ANCHOR_TOPS, DRIVE_EVENTS, HOLDING_STATES, TERMINAL_STATES, Coordinator, _anchored_bytes, \
    _scope_allows, binding_anchor, holds_fence, resolving_decisions
from .store import _require_local_ext4

FORMAT = 1
STEP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ROLES = ("worker", "reviewer", "judgment")
KINDS = ("compat", "author", "bundle", "review", "accept", "export", "setup", "implement", "commit", "verify", "status",
         "spec")
WRAPS = {"bundle": ("author", "verify", "spec"), "review": ("bundle",), "accept": ("review",), "commit": ("implement",),
         "verify": ("commit",)}   # a wrapper step and the kinds it takes
STORY_KINDS = ("setup", "implement", "commit", "verify", "status", "spec")
SKILLS = {"bmad-prd": "prd", "bmad-architecture": "architecture"}
OUT_OF_SLICE = frozenset({"branch", "branches", "loop", "epics", "sprint_status", "sprint-status", "validate"})
MAX_UPDATES = 8
MAX_JUDGMENTS = 8
MAX_CORRECTIONS = 6
PLANNING_MAX_CORRECTIONS = 4   # a planning pipeline validates as before the implementation drive
MAX_TIMEOUT = 86400
MAX_TURNS = 1000   # design D25: a role's turn limit
COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_OUTPUT = 1 << 24
COMPLETE = ("OBSERVED_COMPLETE_CLAIM", "OBSERVED_PARTIAL_CLAIM")
SPEC_READY = "spec-ready:"   # design D24: the source_ref prefix of a carry's decision


def _unsupported(message: str) -> DomainError:
    return DomainError("UNSUPPORTED_STEP", message)


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_INPUT", message)


def _keys(value, required, optional, what: str) -> None:
    if not isinstance(value, dict):
        raise _invalid(f"{what} must be an object")
    extra = set(value) - set(required) - set(optional)
    if extra & OUT_OF_SLICE:
        raise _unsupported(f"{what} uses {', '.join(sorted(extra & OUT_OF_SLICE))}, which is outside this slice")
    if extra or set(required) - set(value):
        raise _invalid(f"{what} fields do not match the pipeline schema")


def _text(value, what: str) -> None:
    if type(value) is not str or not value.strip():
        raise _invalid(f"{what} must be non-empty text")


def _integer(value, low: int, high: int, what: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise _invalid(f"{what} must be an integer from {low} to {high}")


def _role(name: str, value) -> None:
    _keys(value, ("client", "model", "timeout"), ("web", "max_turns"), f"Role {name}")
    if type(value["client"]) is not str:
        raise _invalid(f"Role {name} client must be a registered client name")
    if value["client"] not in clients.REGISTRY:
        raise _unsupported(f"Role {name} names an unknown client")
    _text(value["model"], f"Role {name} model")
    if value["model"].startswith("-"):
        raise _invalid(f"Role {name} model must not look like a flag")
    _integer(value["timeout"], 1, MAX_TIMEOUT, f"Role {name} timeout")
    if "web" in value:   # design D23: web access for a story worker
        if name != "worker":
            raise _invalid("Only the worker role takes web")
        if value["web"] is False:
            raise _invalid("Omit web instead of false")
        if value["web"] is not True:
            raise _invalid("web must be true")
        if value["client"] == clients.COPILOT.name:
            raise _invalid("A copilot worker takes no web")
    if "max_turns" in value:   # design D25: a turn limit per role, for a client that takes one
        _integer(value["max_turns"], 1, MAX_TURNS, f"Role {name} max_turns")
        if clients.get(value["client"]).max_turns_default is None:
            raise _invalid(f"Role {name} client {value['client']} takes no max_turns")


def _head(step_id: str, steps: dict) -> dict:
    """The step a chain of wrapper steps (accept -> review -> bundle -> author, verify -> commit -> implement, or
    spec) starts from."""
    while steps[step_id]["kind"] in WRAPS:
        step_id = steps[step_id]["of"]
    return steps[step_id]


def _source(step_id: str, steps: dict) -> str | None:
    """The skill of the author step that a chain of wrapper steps starts from."""
    return _head(step_id, steps).get("skill")


def _wrapper(step: dict, kinds: dict, roles: dict) -> None:
    """bundle {of: author}, review {of: bundle, reviewer_id}, accept {of: review} and export {to: absolute dir}: thin
    slice-3 steps that run no skill. A skill or intent on them is outside the slice (UNSUPPORTED_STEP), as are the
    out-of-slice keys; shape and references are INVALID_INPUT."""
    sid, kind = step["id"], step["kind"]
    if "skill" in step or "intent" in step:
        raise _unsupported(f"Step {sid}: a {kind} step runs no skill or intent")
    fields = {"bundle": ("of",), "review": ("of", "reviewer_id"), "accept": ("of",), "export": ("to",),
              "commit": ("of",), "verify": ("of",)}[kind]
    _keys(step, ("id", "kind") + fields, (), f"Step {sid}")
    if kind == "export":
        to = step["to"]
        if type(to) is not str or not os.path.isabs(to) or os.path.normpath(to) != to:
            raise _invalid(f"Step {sid} to must be a normalized absolute directory")
        return
    if type(step["of"]) is not str or kinds.get(step["of"]) not in WRAPS[kind]:
        raise _invalid(f"Step {sid} must take an earlier {' or '.join(WRAPS[kind])} step")
    if kind == "review":
        _text(step["reviewer_id"], f"Step {sid} reviewer")
        if "reviewer" not in roles:
            raise _invalid(f"Step {sid} needs a reviewer role")
        if not clients.get(roles["reviewer"]["client"]).write_free_readonly:
            raise _unsupported("The reviewer client has no write-free read-only mode")


def _author(step: dict, kinds: dict, steps: dict) -> None:
    sid = step["id"]
    skill = step.get("skill")
    if type(skill) is not str:
        raise _invalid(f"Step {sid} needs a skill")
    if skill not in SKILLS:
        raise _unsupported(f"Skill {skill} is outside this slice")
    if step.get("intent", "create") != "create":
        raise _unsupported(f"Step {sid} intent is outside this slice; only create is driven")
    architecture = skill == "bmad-architecture"
    required = ("id", "kind", "skill", "slug", "author_id", "inputs") + \
        (("altitude", "purpose") if architecture else ())
    _keys(step, required, ("intent",) + (("prd_from",) if architecture else ()), f"Step {sid}")
    if architecture:
        if type(step["altitude"]) is not str or type(step["purpose"]) is not str:
            raise _invalid(f"Step {sid} altitude and purpose must be text")
        if step["altitude"] != "feature":
            raise _unsupported("Only feature altitude architecture is driven")
        if step["purpose"] not in ("build-substrate", "discussion"):
            raise _invalid(f"Step {sid} purpose is unknown")
    if type(step["slug"]) is not str or not bmad_planning.SLUG.match(step["slug"]):
        raise _invalid(f"Step {sid} slug is invalid")
    _text(step["author_id"], f"Step {sid} author")
    inputs = step["inputs"]
    if not isinstance(inputs, dict) or any(
            type(path) is not str or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
            or role not in bmad_planning.INPUT_ROLES for path, role in inputs.items()):
        raise _invalid(f"Step {sid} inputs must map relative paths to: " + ", ".join(bmad_planning.INPUT_ROLES))
    if "prd_from" in step and (kinds.get(step["prd_from"]) != "accept" or
                               _source(step["prd_from"], steps) != "bmad-prd"):
        raise _invalid(f"Step {sid} prd_from must name an earlier accept step of bmad-prd work")
    if not inputs and "prd_from" not in step:
        raise _invalid(f"Step {sid} create needs a driving input")


def _command(item, what: str) -> None:
    _keys(item, ("name", "argv", "timeout_seconds"), ("cwd",), what)
    if type(item["name"]) is not str or not COMMAND_NAME.match(item["name"]):
        raise _invalid(f"{what} name must be a short lower-case name")
    argv = item["argv"]
    if not isinstance(argv, list) or not argv or any(type(part) is not str or not part for part in argv):
        raise _invalid(f"{what} argv must be a non-empty list of text")
    _integer(item["timeout_seconds"], 1, MAX_TIMEOUT, f"{what} timeout")
    cwd = item.get("cwd")
    if cwd is not None and (type(cwd) is not str or not cwd or PurePosixPath(cwd).is_absolute() or
                            ".." in PurePosixPath(cwd).parts):
        raise _invalid(f"{what} cwd must be a relative directory")


def _project(value) -> None:
    """Design D5: the project block of a story pipeline. `artifacts_dir` is a normalized relative POSIX directory
    without glob characters, so the status files' string-prefix checks (protected, prepare, sync_status) are sound.
    `context` (design D24) lists the relative files a spec author reads first."""
    _keys(value, ("setup", "verify", "env", "commit_identity", "output_limit_bytes"),
          ("rules", "artifacts_dir", "briefs_dir", "context"), "Pipeline project")
    for name in ("setup", "verify"):
        if not isinstance(value[name], list):
            raise _invalid(f"Project {name} must be a list")
        for item in value[name]:
            _command(item, f"Project {name} command")
        if len({item["name"] for item in value[name]}) != len(value[name]):
            raise _invalid(f"Project {name} command names must be distinct")
    if not value["verify"]:   # the verify gate is the point of a story pipeline: baseline and verify run it
        raise _invalid("Project verify needs at least one command")
    if not isinstance(value["env"], list) or any(type(item) is not str or not re.match(r"^[A-Z_][A-Z0-9_]*$", item)
                                                  for item in value["env"]):
        raise _invalid("Project env must list variable names")
    _keys(value["commit_identity"], ("name", "email"), (), "Project commit identity")
    if any(type(value["commit_identity"][key]) is not str or not value["commit_identity"][key].strip()
           for key in ("name", "email")):
        raise _invalid("Project commit identity needs a name and an email")
    _integer(value["output_limit_bytes"], 1024, MAX_OUTPUT, "output_limit_bytes")
    for key in ("rules", "context"):
        paths = value.get(key, [])
        if not isinstance(paths, list) or any(type(item) is not str or not item or PurePosixPath(item).is_absolute() or
                                              ".." in PurePosixPath(item).parts for item in paths):
            raise _invalid(f"Project {key} must be relative file paths")
    for key in ("artifacts_dir", "briefs_dir"):
        _directory(value, key)


def _directory(project: dict, key: str) -> None:
    """A normalized relative POSIX directory without glob characters (design D5 for `artifacts_dir`, D22 for
    `briefs_dir`); `briefs_dir` never lies under `.git` or `.bmad-coordinator`."""
    directory = project.get(key)
    if directory is None:
        return
    if type(directory) is not str or not directory or directory == "." or \
       PurePosixPath(directory).as_posix() != directory or PurePosixPath(directory).is_absolute() or \
       ".." in PurePosixPath(directory).parts or any(char in directory for char in "*?[]"):
        raise _invalid(f"Project {key} must be a normalized relative directory without glob characters")
    if key == "briefs_dir" and PurePosixPath(directory).parts[0] in (".git", ".bmad-coordinator"):
        raise _invalid("Project briefs_dir cannot lie under .git or .bmad-coordinator")


def _story_step(step: dict, kinds: dict, steps: dict) -> None:
    sid, kind = step["id"], step["kind"]
    if kind == "setup":
        _keys(step, ("id", "kind"), (), f"Step {sid}")
        if "setup" in kinds.values():
            raise _invalid("A pipeline has one setup step")
        if "spec" in kinds.values() and not any(item["kind"] == "status" and item["phase"] == "spec"
                                                for item in steps.values()):
            raise _invalid("The setup step follows the spec chain")
    elif kind in ("spec", "implement"):   # design D24: the spec step takes the implement step's fields
        _keys(step, ("id", "kind", "author_id", "story"), (), f"Step {sid}")
        _text(step["author_id"], f"Step {sid} author")
        _keys(step["story"], ("spec_path", "story_key"), (), f"Step {sid} story")
        if any(type(step["story"][key]) is not str or not step["story"][key] for key in ("spec_path", "story_key")):
            raise _invalid(f"Step {sid} story needs spec_path and story_key")
        if kind in kinds.values():
            raise _invalid("A story pipeline has one spec step" if kind == "spec" else
                           "A pipeline has one implement step")
        if kind == "spec" and kinds:
            raise _invalid("The spec step must be the first step")
        if kind == "implement" and ("setup" not in kinds.values() or "status" not in kinds.values()):
            raise _invalid("The setup step and the start status step precede the implement step")
    else:  # status
        _keys(step, ("id", "kind", "phase"), ("of",), f"Step {sid}")
        if step["phase"] not in ("spec", "start", "done"):
            raise _invalid(f"Step {sid} phase must be spec, start or done")
        if step["phase"] == "start" and ("of" in step or "implement" in kinds.values()):
            raise _invalid("The start status step takes no `of` and precedes the implement step")
        if step["phase"] == "start" and "setup" not in kinds.values():   # the baseline runs on the base commit
            raise _invalid("The start status step follows the setup step")
        if step["phase"] == "spec" and (type(step.get("of")) is not str or kinds.get(step["of"]) != "accept" or
                                        _head(step["of"], steps)["kind"] != "spec"):
            raise _invalid("The spec status step takes the spec accept step")
        if step["phase"] == "done" and (type(step.get("of")) is not str or kinds.get(step["of"]) != "accept" or
                                        _head(step["of"], steps)["kind"] != "implement"):
            raise _invalid("The done status step takes an earlier accept step")


def validate(value) -> dict:
    """The canonical pipeline, or UNSUPPORTED_STEP (exit 4) for a kind, skill, intent or client outside the slice
    and INVALID_INPUT (exit 2) for shape, type, reference or budget errors. Pure; writes nothing."""
    _keys(value, ("kind", "format", "feature_id", "mandate_revision", "roles", "budgets", "steps"),
          ("accept_on_high", "project", "spec_auto_accept"), "Pipeline")
    for key in ("accept_on_high", "spec_auto_accept"):
        if type(value.get(key, False)) is not bool:
            raise _invalid(f"{key} must be true or false")
    if value["kind"] != "drive-pipeline" or type(value["format"]) is not int or value["format"] != FORMAT:
        raise _invalid("Pipeline kind or format is not supported")
    _text(value["feature_id"], "Pipeline feature")
    _integer(value["mandate_revision"], 1, 1 << 62, "Pipeline mandate revision")
    roles = value["roles"]
    if not isinstance(roles, dict) or "worker" not in roles or set(roles) - set(ROLES):
        raise _invalid("Pipeline roles must include worker and name only worker, reviewer and judgment")
    for name, role in roles.items():
        _role(name, role)
    _keys(value["budgets"], ("max_updates", "max_judgments"), ("max_corrections",), "Pipeline budgets")
    _integer(value["budgets"]["max_updates"], 0, MAX_UPDATES, "max_updates")
    _integer(value["budgets"]["max_judgments"], 0, MAX_JUDGMENTS, "max_judgments")
    cap = (MAX_CORRECTIONS, "story") if "project" in value else (PLANNING_MAX_CORRECTIONS, "planning")
    corrections = value["budgets"].get("max_corrections", 0)
    if type(corrections) is not int or not 0 <= corrections <= cap[0]:
        raise _invalid(f"max_corrections must be an integer from 0 to {cap[0]} (the {cap[1]} pipeline cap)")
    if value["budgets"]["max_judgments"] > 0 and "judgment" not in roles:
        raise _invalid("max_judgments > 0 needs a judgment role")
    if "judgment" in roles and not clients.get(roles["judgment"]["client"]).write_free_readonly:
        raise _unsupported("The judgment client has no write-free read-only mode (isolated runs are read-only)")
    if "project" in value:
        _project(value["project"])
    steps = value["steps"]
    if not isinstance(steps, list) or not steps:
        raise _invalid("Pipeline steps must be a non-empty list")
    kinds, authors, seen = {}, set(), {}
    for step in steps:
        if not isinstance(step, dict) or type(step.get("kind")) is not str:
            raise _invalid("Every step needs a kind")
        if step["kind"] not in KINDS:
            raise _unsupported(f"Step kind {step['kind']} is outside this slice")
        sid = step.get("id")
        if type(sid) is not str or not STEP_ID.match(sid) or sid in kinds:
            raise _invalid("Step ids must be distinct short lowercase names")
        if "export" in kinds.values():
            raise _invalid("The export step must be the last step")
        if step["kind"] == "compat":
            _keys(step, ("id", "kind"), (), f"Step {sid}")
            if "compat" in kinds.values():
                raise _invalid("A pipeline has one compat step")
        elif step["kind"] == "author":
            _author(step, kinds, seen)
            if "compat" not in kinds.values():
                raise _invalid("The compat step must precede every author step")
            if (step["skill"], step["slug"]) in authors:
                raise _invalid("A pipeline has one author step per skill and slug")
            authors.add((step["skill"], step["slug"]))
        elif step["kind"] in ("setup", "implement", "status", "spec"):
            _story_step(step, kinds, seen)
        else:
            _wrapper(step, kinds, roles)
            if not {"compat", "spec", "implement"} & set(kinds.values()):
                raise _invalid("The compat step (planning) or the spec or implement step (story) must precede every "
                               "other step")
            if "of" in step and any(item.get("of") == step["of"] for item in seen.values()):
                raise _invalid(f"Step {step['of']} is taken by more than one step")
        kinds[sid] = step["kind"]
        seen[sid] = step
    named = {step["author_id"] for step in steps if step["kind"] in ("author", "spec", "implement")}
    if any(step["kind"] == "review" and step["reviewer_id"] in named for step in steps):
        raise _invalid("A reviewer must differ from every author of the pipeline")
    story = "implement" in kinds.values()
    if roles["worker"].get("web") and not story:
        raise _invalid("web needs a story pipeline")
    if "spec_auto_accept" in value and not story:
        raise _invalid("spec_auto_accept needs a story pipeline")
    if story and any(kind == "author" for kind in kinds.values()):
        raise _invalid("A pipeline drives either planning author steps or one story, never both")
    if story and "compat" in kinds.values():   # design D22: the story path reads no BMAD installation
        raise _invalid("A story pipeline takes no compat step")
    if story != ("project" in value):
        raise _invalid("A story pipeline needs the project block; a planning pipeline takes none")
    # `story add` from an epic reads the default artifacts directory (design D24)
    if story and "spec" in kinds.values() and \
       implementation._artifacts_dir(value["project"]) != implementation.ARTIFACTS_DIR:
        raise _invalid("A story added from an epic keeps the default artifacts_dir")
    if story:
        phases = [step["phase"] for step in steps if step["kind"] == "status"]
        expected = ["spec", "start", "done"] if "spec" in kinds.values() else ["start", "done"]
        if phases != expected or "commit" not in kinds.values() or "verify" not in kinds.values() or \
           "accept" not in kinds.values():
            raise _invalid("A story pipeline runs status start, implement, commit, verify, bundle, review, accept, "
                           "status done")
    elif any(kind in STORY_KINDS for kind in kinds.values()):
        raise _invalid("Story steps need an implement step")
    return json.loads(canonical(value))


def digest(value) -> str:
    return hashlib.sha256(canonical(validate(value))).hexdigest()


def step_key(pipeline_sha256: str, step_id: str, n: int) -> str:
    key = f"drv-{pipeline_sha256[:12]}-{step_id}-{n}"
    if not bmad_planning.KEY.match(key):
        raise _invalid("Step key does not fit the planning key grammar")
    return key


def _read_sealed(base: Path, ref: str, expected: str) -> bytes:
    """The same reader anchor_event re-hashes with; any defect is unavailable evidence here."""
    try:
        return _anchored_bytes(base, ref, expected, ANCHOR_TOPS + ("resolutions",))
    except DomainError as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed driver file is unavailable or differs from its anchor") from exc


def read_sealed_json(base: Path, ref: str, expected: str) -> dict:
    data = _read_sealed(base, ref, expected)
    value = json.loads(data)
    if not isinstance(value, dict) or canonical(value) != data:
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed driver file is not canonical JSON")
    return value


def load(base: Path, pipeline_sha256: str) -> dict:
    return validate(read_sealed_json(base, f"pipelines/{pipeline_sha256}.json", pipeline_sha256))


def _bound_events(snapshot: dict, feature_id: str, revision: int) -> list[dict]:
    return [row for row in snapshot["events"] if row["kind"] == "pipeline.bound" and
            row["subject_id"] == feature_id and row["source_id"] == f"r{revision}"]


def _export_target(snapshot: dict, to: str) -> None:
    """An export directory must exist (not a symlink) on local ext4, the exporter's own filesystem rule, and lie
    outside every feature writable scope, including the checkpoint files it will publish there."""
    if os.path.islink(to) or not os.path.isdir(to):
        raise _invalid("Export directory must be an existing directory")
    target = os.path.realpath(to)
    try:
        _require_local_ext4(Path(target))
    except DomainError as exc:
        raise DomainError("UNSUPPORTED_FILESYSTEM", "Export directory must be on local ext4") from exc
    for feature in snapshot["features"]:
        if not feature["worktree"]:
            continue
        worktree = os.path.realpath(feature["worktree"])
        if os.path.commonpath([worktree, target]) != worktree:
            continue
        relative = PurePosixPath(os.path.relpath(target, worktree))
        scopes = json.loads(feature["writable_paths_json"])
        published = relative / "checkpoint-r0-0"  # glob scopes match across `/`, so probe the published files too
        probes = (relative, relative / "checkpoint", published / "checkpoint.json", published / "checkpoint.md")
        if any(_scope_allows(path.as_posix(), scopes) for path in probes):
            raise _invalid("Export directory lies inside a feature writable scope")


def check_bindable(snapshot: dict, value: dict) -> bool:
    """Read-only pre-claim check; True when this exact pipeline is already bound at the current revision. Judgments
    (max_judgments > 0) need the mandate authority list `authority.delegated: [{id, text}]` (UNSUPPORTED_STEP); an
    export directory must exist outside every feature writable scope (INVALID_INPUT) on local ext4
    (UNSUPPORTED_FILESYSTEM). Design D24: a `spec` step binds only for a feature added from an epic (`provisional`),
    which needs one, or carried past its spec (a `spec` status receipt), and names the implement step's story
    (STALE_INPUT)."""
    value = validate(value)
    feature = next((row for row in snapshot["features"] if row["id"] == value["feature_id"]), None)
    if feature is None:
        raise DomainError("NOT_FOUND", "Pipeline feature does not exist")
    for step in value["steps"]:
        if step["kind"] == "export":
            _export_target(snapshot, step["to"])
    if feature["mandate_revision"] != value["mandate_revision"]:
        raise DomainError("STALE_INPUT", "Pipeline mandate revision is not the feature's current revision")
    if value["budgets"]["max_judgments"] > 0 and judgment.authority_ids(json.loads(feature["authority_json"])) is None:
        raise _unsupported("max_judgments > 0 needs a mandate authority list authority.delegated: [{id, text}]")
    implement = next((step for step in value["steps"] if step["kind"] == "implement"), None)
    if implement is not None:
        constraints = json.loads(feature["constraints_json"])
        spec = next((step for step in value["steps"] if step["kind"] == "spec"), None)
        provisional = isinstance(constraints, dict) and constraints.get("provisional") is True
        carried = any(row["kind"] == "story.status" and row["subject_id"] == feature["id"] and
                      row["source_id"] == "spec" for row in snapshot["events"])
        if spec is not None and not provisional and not carried:
            raise DomainError("STALE_INPUT", "A spec step needs a story added from an epic")
        if provisional and spec is None:
            raise DomainError("STALE_INPUT", "A story added from an epic needs a spec step")
        if spec is not None and spec["story"] != implement["story"]:
            raise DomainError("STALE_INPUT", "The spec and implement steps name different stories")
        if not isinstance(constraints, dict) or (constraints.get("story_key"), constraints.get("spec_path")) != \
           (implement["story"]["story_key"], implement["story"]["spec_path"]):
            raise DomainError("STALE_INPUT", "The implement step names another story than the feature")
    bound = _bound_events(snapshot, value["feature_id"], value["mandate_revision"])
    sha = hashlib.sha256(canonical(value)).hexdigest()
    if any(row["payload_digest"] != sha for row in bound):
        raise DomainError("IDEMPOTENCY_CONFLICT", "Another pipeline is bound at this mandate revision")
    return bool(bound)


def bind(repo, store, owner: OwnerRef, value: dict, sealed=None) -> dict:
    """Seal pipelines/<sha>.json (identical bytes accepted) and anchor `pipeline.bound`; an identical rebind is a
    no-op, another digest at the same mandate revision is IDEMPOTENCY_CONFLICT."""
    value = validate(value)
    data = canonical(value)
    sha = hashlib.sha256(data).hexdigest()
    ref = f"pipelines/{sha}.json"
    if check_bindable(store.read_export_snapshot(owner), value):
        return {"sha256": sha, "ref": ref, "bound": False}
    base = store.path.parent
    directory = base / "pipelines"
    if directory.is_symlink():
        raise DomainError("UNSAFE_PATH", "Pipeline directory is unsafe")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        private_file(directory / f"{sha}.json", data)
    except FileExistsError:
        pass
    if _read_sealed(base, ref, sha) != data:
        raise DomainError("BLOCKED_EVIDENCE", "Pipeline file conflicts with different bytes")
    if sealed is not None:
        sealed()
    Coordinator(store).anchor_event(owner, "pipeline.bound", value["feature_id"],
                                    (f"r{value['mandate_revision']}", ref, sha))
    return {"sha256": sha, "ref": ref, "bound": True}


def active(base: Path, snapshot: dict, feature_id: str) -> dict | None:
    """The pipeline bound at the feature's current mandate revision, or None (none bound, or the mandate was
    revised since: a revision needs a new binding)."""
    feature = next((row for row in snapshot["features"] if row["id"] == feature_id), None)
    if feature is None:
        return None
    bound = _bound_events(snapshot, feature_id, feature["mandate_revision"])
    if not bound:
        return None
    row = bound[-1]
    return {"sha256": row["payload_digest"], "ref": row["evidence_ref"], "revision": feature["mandate_revision"],
            "value": load(base, row["payload_digest"])}


@dataclass(frozen=True)
class StepState:
    step_id: str
    kind: str
    state: str                    # pending | ready | in-flight | waiting-decision | satisfied | needs-human
    reason: str | None = None     # a closed needs-human reason code
    action: dict | None = None    # ready only
    subjects: dict = field(default_factory=dict)

    def view(self) -> dict:
        return {"id": self.step_id, "kind": self.kind, "state": self.state, "reason": self.reason,
                "subjects": self.subjects, "action": self.action and {key: value for key, value in self.action.items()
                                                                      if key != "spec"}}


def gather(repo, store, snapshot: dict, value: dict, generation: int) -> dict:
    """Read-only facts for derive: discovery (planning pipelines only; a story pipeline gathers no contract facts,
    design D22), step workspaces, planning, implementation and review bindings, observed profile payloads, the
    feature's judgments (from anchors and digest-valid receipts only), its bundle manifests (with a code bundle's
    commit), verify's state of every acceptance, the export pointer with its directory under each export step's `to`
    and, for a story pipeline, the story receipts (`story_facts`); with a `spec` status receipt (design D24) also
    whether its commit is on the branch, and with a spec step the digest and the clauses of each spec attempt's spec
    at its commit."""
    feature = next(row for row in snapshot["features"] if row["id"] == value["feature_id"])
    worktree = Path(feature["worktree"])
    story = any(step["kind"] == "implement" for step in value["steps"])
    report = None if story else contract.discover(worktree, ".agents/skills")   # design D22
    facts = {"generation": generation, "workspaces": {}, "bindings": {}, "unbound": [], "payloads": {},
             "reviews": {}, "bundles": {}, "acceptances": {}, "export": None,
             "contract": None if report is None else {"supported": report.supported,
                                                      "fingerprint": report.fingerprint_sha256,
                                                      "reasons": list(report.reasons)[:5]},
             "judgments": judgment.facts(store.path.parent, snapshot, feature["id"])}
    kinds = {step["kind"] for step in value["steps"]}
    if "bundle" in kinds:
        captured = {row["subject_id"]: row["revision"] for row in snapshot["events"]
                    if row["kind"] == "bundle.captured"}
        for row in snapshot["bundles"]:
            if row["feature_id"] != feature["id"]:
                continue
            try:
                manifest = bundles.read_manifest(Path(row["path"]), row["manifest_digest"])
            except (DomainError, OSError, KeyError, TypeError, ValueError):
                continue  # unreadable evidence never satisfies a step
            facts["bundles"][row["id"]] = {
                "revision": captured.get(row["id"], 0), "mandate_revision": row["mandate_revision"],
                "sources": json.loads(row["source_assignments_json"]),
                "artifacts": [item["path"] for item in manifest["files"] if item["kind"] == "artifact"],
                "commit": manifest["commit"] if isinstance(manifest.get("commit"), dict) else None}
    gates = {}
    if "accept" in kinds and snapshot["acceptances"]:
        checked = Coordinator(store).verify().checked
        facts["acceptances"] = {item["acceptance_id"]: item["current"] for item in checked}
        gates = {item["acceptance_id"]: list(item.get("gate") or []) for item in checked}
    if "export" in kinds:
        meta = snapshot if "export_revision" in snapshot else store.read_snapshot()
        name = meta["export_dir_name"]
        facts["export"] = {"revision": meta["export_revision"], "dir_name": name, "present": {
            step["to"]: bool(name) and os.path.isdir(os.path.join(step["to"], name)) and
            not os.path.islink(os.path.join(step["to"], name)) for step in value["steps"] if step["kind"] == "export"}}
    root = None
    if not story:
        try:
            root = contract.central_path(worktree, "modules.bmm.planning_artifacts")
        except DomainError:
            root = None
    for step in value["steps"]:
        if step["kind"] == "author" and root is not None:
            facts["workspaces"][step["id"]] = f"{root}/features/{step['slug']}/{SKILLS[step['skill']]}"
    base = store.path.parent
    for row in snapshot["assignments"]:
        if row["feature_id"] != feature["id"]:
            continue
        try:
            name, bound = binding_anchor(snapshot["events"], row["id"])
        except DomainError:
            facts["unbound"].append(row["id"])
            continue
        if name == bmad_planning.REVIEW and bound is not None:
            try:
                facts["reviews"][row["id"]] = read_binding(base, bound["evidence_ref"], bound["payload_digest"])
            except (DomainError, KeyError, TypeError, ValueError):
                pass
            continue
        if name not in (bmad_planning.PLAN, *implementation.STORY_PROFILES):
            continue
        try:
            facts["bindings"][row["id"]] = read_binding(base, bound["evidence_ref"], bound["payload_digest"])
            ret = json.loads(row["return_json"]) if row["return_json"] else None
            if ret and ret.get("provenance_class") == "observed":
                facts["payloads"][row["id"]] = read_receipt(repo, ret["event_ref"],
                                                            ret["payload_digest"]).get("profile_payload")
        except (DomainError, KeyError, TypeError, ValueError):
            facts["payloads"].pop(row["id"], None)
    if "implement" in kinds:
        facts["story"] = {**implementation.story_facts(base, snapshot, feature["id"]), "acceptance_gates": gates}
        if "spec" in facts["story"]["status"]:
            facts["story"]["spec_on_branch"] = _spec_on_branch(worktree, facts["story"])
        if "spec" in kinds:
            facts["story"]["specs"] = _specs(repo, facts)
    else:
        facts["story"] = None
    return facts


def _spec_on_branch(worktree: Path, story: dict) -> bool:
    """Ruling R5: the `spec` status commit is HEAD or an ancestor of HEAD (the status currency's Git call)."""
    sha = (story["status"].get("spec") or {}).get("sha")
    return bool(sha) and implementation._git_run(worktree, ["merge-base", "--is-ancestor", sha, "HEAD"],
                                                 check=False).returncode == 0


def _specs(repo, facts: dict) -> dict:
    """Design D24: for the spec at the commit of each spec attempt, `{digest, clauses}`: `bmad_spec_author.digest`
    and the `S<n>: <text>` clauses the carry would delegate (`implementation.clauses`, I1), which the `spec-decisions`
    stops render (a question, or the approval of a reviewed spec); both None when the spec does not parse."""
    found = {}
    for assignment_id, binding in facts["bindings"].items():
        commit = facts["story"]["commits"].get(assignment_id) or {}
        if binding.get("profile") != implementation.SPEC_AUTHOR or not commit.get("sha"):
            continue
        try:
            text = implementation._read_at(repo, commit["sha"], binding["spec_path"], "Story spec")
            clauses = implementation.clauses(implementation.parse_spec(text), [])
            found[assignment_id] = {"digest": bmad_spec_author.digest(text),
                                    "clauses": [f"{item['id']}: {item['text']}" for item in clauses]}
        except DomainError:
            found[assignment_id] = {"digest": None, "clauses": None}
    return found


def fence_holder(snapshot: dict, feature_id: str, exclude: str | None = None) -> tuple[str, str] | None:
    """(holder id, reason) for an attempt that fences the feature's worktree (the core fence rule), else None."""
    worktrees = {row["id"]: os.path.realpath(row["worktree"]) for row in snapshot["features"] if row["worktree"]}
    mine = worktrees.get(feature_id)
    for row in snapshot["assignments"]:
        if row["id"] == exclude or worktrees.get(row["feature_id"]) != mine:
            continue
        if holds_fence(row["state"], row["return_json"]):
            if row["feature_id"] != feature_id:
                return row["id"], "fenced-by-other"
            return row["id"], "attempt-unknown" if row["state"] in HOLDING_STATES else "unobserved-return"
    return None


def _spec(step: dict, feature_id: str, key: str, compat_id: str, client: str, **extra) -> dict:
    spec = {"feature_id": feature_id, "key": key, "skill": step["skill"], "author_id": step["author_id"],
            "compat_decision_id": compat_id, "client": client, **extra}
    if step["skill"] == "bmad-architecture":
        spec.update(altitude=step["altitude"], purpose=step["purpose"])
    return spec


def _compat(step: dict, snapshot: dict, facts: dict, feature: dict) -> StepState:
    found = facts["contract"]
    subjects = {"step_id": step["id"]}
    if not found["supported"]:
        return StepState(step["id"], "compat", "needs-human", "compat-review", None,
                         {**subjects, "reasons": found["reasons"]})
    fingerprint = found["fingerprint"]
    rows = {row["id"]: row for row in snapshot["assignments"]}
    drift = sorted(aid for aid, binding in facts["bindings"].items()
                   if binding.get("fingerprint_sha256") != fingerprint and
                   rows[aid]["state"] != "cancelled-before-launch" and
                   rows[aid]["mandate_revision"] == feature["mandate_revision"])
    if drift:
        return StepState(step["id"], "compat", "needs-human", "contract-drift", None,
                         {**subjects, "assignment_ids": drift})
    decision = contract.compat_decision(snapshot, feature["id"], feature["mandate_revision"], fingerprint)
    if decision is None:
        return StepState(step["id"], "compat", "needs-human", "compat-missing", None,
                         {**subjects, "fingerprint": fingerprint})
    return StepState(step["id"], "compat", "satisfied", None, None,
                     {**subjects, "compat_decision_id": decision, "fingerprint": fingerprint})


def _own(key: str, prefix: str) -> bool:
    """A step owns only its keys `drv-<pipeline12>-<step>-<n>`: step `prd` never claims `prd-review-1`."""
    return key.startswith(prefix) and key[len(prefix):].isdigit()


def _next_key(sha: str, sid: str, rows) -> str:
    prefix = f"drv-{sha[:12]}-{sid}-"
    used = [int(row["idempotency_key"][len(prefix):]) for row in rows if _own(row["idempotency_key"], prefix)]
    return step_key(sha, sid, max(used, default=0) + 1)


def _author_state(step, sha, snapshot, facts, feature, compat_id, value, prd_acceptance=None) -> StepState:
    sid, rev = step["id"], feature["mandate_revision"]
    client = value["roles"]["worker"]["client"]
    subjects = {"step_id": sid}
    workspace = facts["workspaces"].get(sid)
    if workspace is None:
        return StepState(sid, "author", "needs-human", "stale-input", None,
                         {**subjects, "detail": "planning workspace cannot be resolved"})
    prepared = {row["subject_id"]: row["revision"] for row in snapshot["events"]
                if row["kind"] == "assignment.prepared"}
    rows = [row for row in snapshot["assignments"] if row["feature_id"] == feature["id"]]
    live = [row for row in rows if row["state"] != "cancelled-before-launch" and row["mandate_revision"] == rev]
    bound = sorted((row for row in live if facts["bindings"].get(row["id"], {}).get("skill") == step["skill"] and
                    facts["bindings"][row["id"]].get("workspace") == workspace), key=lambda row: prepared[row["id"]])
    prefix = f"drv-{sha[:12]}-{sid}-"
    key = _next_key(sha, sid, rows)
    half = sorted((row for row in live if row["id"] in facts["unbound"] and row["state"] == "prepared" and
                   _own(row["idempotency_key"], prefix)), key=lambda row: prepared[row["id"]])
    state = _head_state(step, sid, snapshot, facts, bound, key, compat_id, client, value, subjects, feature["id"],
                        sha, workspace, prd_acceptance)
    if not half:
        return state
    stale = half[-1]
    if state.state == "ready" and state.action["kind"] == "prepare":
        spec = {**state.action["spec"], "key": stale["idempotency_key"]}
        return StepState(sid, "author", "ready", None, {**state.action, "spec": spec, "finishes": stale["id"]},
                         {**state.subjects, "assignment_id": stale["id"]})
    return StepState(sid, "author", "ready", None, {"kind": "cancel-stale", "assignment_id": stale["id"]},
                     {**subjects, "assignment_id": stale["id"]})


def _judgment_state(sid, sha, request, facts, value, subjects, kind="author", stop=None) -> StepState:
    """An open request without conflicts: judge it (budget left, never judged), decide from a valid in-mandate answer,
    or a needs-human reason. The latest judgment of the request decides; an unknown or failed-preflight judgment is
    never replaced, and an observed verdict is final (a restart never re-asks). For a story an observed refusal is a
    product decision (design D16) and carries the question and options; for a spec attempt (`stop`, design D24) it
    is the `spec-decisions` stop with the given subjects."""
    story = kind != "author"
    judged = facts.get("judgments", {})
    mine = [item for item in judged.values() if item["request_id"] == request["id"]]
    if not mine:
        prefix = f"drv-{sha[:12]}-"
        used = sum(1 for item in judged.values()  # max_judgments counts every launch
                   if item["key"].startswith(prefix) and item["state"] != "failed-preflight")
        if used >= value["budgets"]["max_judgments"]:
            return StepState(sid, kind, "needs-human", "judgment-budget", None, subjects)
        n = 1 + sum(1 for item in judged.values() if item["key"].startswith(f"{prefix}{sid}-j"))
        action = {"kind": "judge", "request_id": request["id"], "key": judgment.key_for(sha, sid, n), "fresh": True}
        return StepState(sid, kind, "ready", None, action, {**subjects, "judgment_key": action["key"]})
    last = mine[-1]
    subjects = {**subjects, "judgment_key": last["key"]}
    if last["state"] == "reserved":
        if last["launched"]:
            return StepState(sid, kind, "in-flight", "judgment-unknown", None, subjects)
        action = {"kind": "judge", "request_id": request["id"], "key": last["key"], "fresh": False}
        return StepState(sid, kind, "ready", None, action, subjects)
    if last["state"] == "observed":
        if last["verdict"] in ("answer", "defer") and last["valid"]:
            return StepState(sid, kind, "ready", None,
                             {"kind": "decide", "request_id": request["id"], "key": last["key"]}, subjects)
        if stop is not None:
            return StepState(sid, kind, "needs-human", "spec-decisions", None, stop)
        refused = {**subjects, "defects": last["defects"][:8], "refusal": (last.get("refusal") or "")[:400]}
        if story:
            question = json.loads(request["question_json"])
            refused.update(question=(question.get("question") or "")[:400], options=(question.get("options") or [])[:8])
        return StepState(sid, kind, "needs-human", "product-decision" if story else "question-refused", None, refused)
    if last["state"] == "failed-preflight":
        return StepState(sid, kind, "needs-human", "judgment-preflight", None,
                         {**subjects, "detail": last["reason"]})
    return StepState(sid, kind, "needs-human", "judgment-unknown", None, {**subjects, "detail": last["reason"]})


def _correction(step, sid, snapshot, facts, bound, head, key, compat_id, client, value, subjects, feature_id,
                workspace) -> StepState | None:
    """Correction-round design D2: prepare a correction of an observed completed head when the newest bundle of this
    workspace naming it at the current revision has an observed review with blocking findings, no acceptance, and
    the step's corrections are below `max_corrections`. Otherwise None: the step is satisfied, and the accept step
    reports the stop with the count. An unreadable review record (`/`) never triggers a correction. The spec carries
    the frozen inputs and PRD acceptance of the chain's create (a request continuation binds none) and the decisions
    that resolved the chain's requests at this revision, through the existing brief sections (ruling R6)."""
    budget = value["budgets"].get("max_corrections", 0)
    if budget <= 0:
        return None
    revision = head["mandate_revision"]
    found = sorted((item["revision"], bundle_id) for bundle_id, item in facts["bundles"].items()
                   if item["mandate_revision"] == revision and head["id"] in item["sources"] and item["artifacts"] and
                   all(path.startswith(workspace + "/") for path in item["artifacts"]))
    if not found:
        return None
    bundle_id = found[-1][1]
    if any(row["bundle_id"] == bundle_id for row in snapshot["acceptances"]):
        return None
    recorded = {row["subject_id"]: row["revision"] for row in snapshot["events"] if row["kind"] == "review.recorded"}
    observed = sorted((recorded.get(row["id"], 0), row["id"], row["finding_json"]) for row in snapshot["reviews"]
                      if row["bundle_id"] == bundle_id and row["provenance_class"] == "observed")
    if not observed:
        return None
    _, review_id, finding_json = observed[-1]
    blocking = blocking_findings(finding_json, value.get("accept_on_high", False))
    if not blocking or "/" in blocking or \
       corrections_used(snapshot, facts, feature_id, workspace, revision) >= budget:
        return None
    chain = {row["id"] for row in bound}
    origin = facts["bindings"][head["id"]]
    while (origin.get("continues") or {}).get("update_of") in chain:
        origin = facts["bindings"][origin["continues"]["update_of"]]
    roles = origin.get("input_roles") or {}
    asked = {row["id"] for row in snapshot["decision_requests"] if row["assignment_id"] in chain}
    decisions = [{"id": row["id"], "text": "", "source_kind": row["source_kind"], "source_ref": row["source_ref"]}
                 for row in sorted(snapshot["decisions"], key=lambda row: row["id"])
                 if row["feature_id"] == feature_id and row["mandate_revision"] == revision and
                 row["request_id"] in asked]
    extra = {"prd_acceptance": origin["prd_acceptance"]} if origin.get("prd_acceptance") else {}
    spec = _spec(step, feature_id, key, compat_id, client, intent="update", update_of=head["id"],
                 corrects_review=review_id, finding_ids=blocking, inputs=sorted(roles), input_roles=dict(roles),
                 decisions=decisions, **extra)
    return StepState(sid, "author", "ready", None, {"kind": "prepare", "intent": "update", "spec": spec},
                     {**subjects, "bundle_id": bundle_id, "review_id": review_id, "finding_ids": blocking})


def _head_state(step, sid, snapshot, facts, bound, key, compat_id, client, value, subjects, feature_id,
                sha, workspace, prd_acceptance=None) -> StepState:
    if not bound:
        extra = {"prd_acceptance": prd_acceptance} if prd_acceptance else {}
        spec = _spec(step, feature_id, key, compat_id, client, intent="create", slug=step["slug"],
                     inputs=list(step["inputs"]), input_roles=dict(step["inputs"]), **extra)
        return StepState(sid, "author", "ready", None, {"kind": "prepare", "intent": "create", "spec": spec}, subjects)
    head = bound[-1]
    subjects = {**subjects, "assignment_id": head["id"]}
    state = head["state"]
    if state == "prepared":
        if head["owner_generation"] != facts["generation"]:
            return StepState(sid, "author", "ready", None, {"kind": "cancel-stale", "assignment_id": head["id"]},
                             subjects)
        return StepState(sid, "author", "ready", None, {"kind": "launch", "assignment_id": head["id"],
                                                        "role": "worker"}, subjects)
    if state in HOLDING_STATES:
        return StepState(sid, "author", "in-flight", "attempt-unknown", None, {**subjects, "holder": head["id"]})
    if state == "resolved-unknown":
        return StepState(sid, "author", "needs-human", "attempt-resolved", None, subjects)
    ret = json.loads(head["return_json"]) if head["return_json"] else {}
    payload = facts["payloads"].get(head["id"])
    if state not in TERMINAL_STATES or ret.get("provenance_class") != "observed" or not isinstance(payload, dict):
        return StepState(sid, "author", "needs-human", "unobserved-return", None, {**subjects, "holder": head["id"]})
    if payload.get("verdict") == "NEEDS_COMPAT_REVIEW":
        return StepState(sid, "author", "needs-human", "compat-review", None, subjects)
    if state == "completed-claim":
        if payload.get("verdict") in COMPLETE:
            correction = _correction(step, sid, snapshot, facts, bound, head, key, compat_id, client, value, subjects,
                                     feature_id, workspace)
            return correction or StepState(sid, "author", "satisfied", None, None, subjects)
        return StepState(sid, "author", "needs-human", "unobserved-return", None, subjects)
    if state == "blocked":
        return StepState(sid, "author", "needs-human", "worker-blocked", None, subjects)
    requests = [row for row in snapshot["decision_requests"] if row["assignment_id"] == head["id"]]
    if not requests:
        return StepState(sid, "author", "needs-human", "unobserved-return", None, subjects)
    request = requests[0]
    subjects["request_id"] = request["id"]
    if request["state"] == "open":
        if payload.get("conflicts"):
            return StepState(sid, "author", "needs-human", "question-conflict", None, subjects)
        return _judgment_state(sid, sha, request, facts, value, subjects)
    resolver = next((row for row in snapshot["decisions"] if row["id"] == request["resolution_decision_id"]), None)
    if resolver is None or resolver["mandate_revision"] != head["mandate_revision"]:
        return StepState(sid, "author", "needs-human", "stale-input", None,
                         {**subjects, "detail": "request resolved at another mandate revision"})
    updates = sum(1 for row in bound if facts["bindings"][row["id"]].get("intent") == "update" and
                  not (facts["bindings"][row["id"]].get("continues") or {}).get("corrects_review"))
    if updates >= value["budgets"]["max_updates"]:
        return StepState(sid, "author", "needs-human", "update-budget", None, subjects)
    spec = _spec(step, feature_id, key, compat_id, client, intent="update", update_of=head["id"],
                 continues_request=request["id"])
    return StepState(sid, "author", "ready", None, {"kind": "prepare", "intent": "update", "spec": spec}, subjects)


def _bundle_state(step, snapshot, facts, feature, done) -> StepState:
    """Satisfied when a bundle of the author step's workspace at the current revision names its head (by binding
    content, whoever captured it); otherwise ready to bundle the workspace. A code bundle (of a verify step, or the
    spec bundle of the spec step: design D24) is satisfied by a bundle at the current revision naming the step's head
    whose manifest carries its commit."""
    sid = step["id"]
    if done[step["of"]].kind in ("verify", "spec"):
        wrapped = done[step["of"]]
        head, sha = wrapped.subjects["assignment_id"], wrapped.subjects["commit_sha"]
        subjects = {"step_id": sid, "assignment_id": head, "commit_sha": sha, "code": True,
                    **({"spec": True} if wrapped.kind == "spec" else {})}
        found = sorted((item["revision"], bundle_id) for bundle_id, item in facts["bundles"].items()
                       if item["mandate_revision"] == feature["mandate_revision"] and head in item["sources"] and
                       (item.get("commit") or {}).get("sha") == sha)
        if found:
            return StepState(sid, "bundle", "satisfied", None, None, {**subjects, "bundle_id": found[-1][1]})
        return StepState(sid, "bundle", "ready", None, {"kind": "bundle", "commit": True, "assignment_id": head},
                         subjects)
    author = done[step["of"]]
    head, workspace = author.subjects["assignment_id"], facts["workspaces"][step["of"]]
    subjects = {"step_id": sid, "assignment_id": head}
    found = sorted((item["revision"], bundle_id) for bundle_id, item in facts["bundles"].items()
                   if item["mandate_revision"] == feature["mandate_revision"] and head in item["sources"] and
                   item["artifacts"] and all(path.startswith(workspace + "/") for path in item["artifacts"]))
    if found:
        return StepState(sid, "bundle", "satisfied", None, None, {**subjects, "bundle_id": found[-1][1]})
    return StepState(sid, "bundle", "ready", None, {"kind": "bundle", "workspace": workspace}, subjects)


def _review_state(step, sha, snapshot, facts, feature, compat_id, value, done) -> StepState:
    """Satisfied by a recorded observed review of the bundle. Otherwise the head is the newest non-cancelled bound
    review assignment of that bundle (binding content), ordered by its prepared-event revision: prepare it, launch
    it with the reviewer role, or record its observed completed return; anything else needs a human."""
    sid, bundle_id = step["id"], done[step["of"]].subjects["bundle_id"]
    subjects = {"step_id": sid, "bundle_id": bundle_id}
    recorded = {row["subject_id"]: row["revision"] for row in snapshot["events"] if row["kind"] == "review.recorded"}
    observed = sorted((recorded.get(row["id"], 0), row["id"], row["critical_open"]) for row in snapshot["reviews"]
                      if row["bundle_id"] == bundle_id and row["provenance_class"] == "observed")
    if observed:
        _, review_id, critical = observed[-1]
        return StepState(sid, "review", "satisfied", None, None,
                         {**subjects, "review_id": review_id, "critical_open": bool(critical)})
    prepared = {row["subject_id"]: row["revision"] for row in snapshot["events"]
                if row["kind"] == "assignment.prepared"}
    rows = [row for row in snapshot["assignments"] if row["feature_id"] == feature["id"]]
    live = [row for row in rows if row["state"] != "cancelled-before-launch" and
            row["mandate_revision"] == feature["mandate_revision"]]
    bound = sorted((row for row in live if facts["reviews"].get(row["id"], {}).get("bundle_id") == bundle_id),
                   key=lambda row: prepared[row["id"]])
    prefix = f"drv-{sha[:12]}-{sid}-"
    half = sorted((row for row in live if row["id"] in facts["unbound"] and row["state"] == "prepared" and
                   _own(row["idempotency_key"], prefix)), key=lambda row: prepared[row["id"]])
    spec = {"bundle_id": bundle_id, "reviewer_id": step["reviewer_id"], "key": _next_key(sha, sid, rows),
            "client": value["roles"]["reviewer"]["client"]}
    if compat_id is not None and not done[step["of"]].subjects.get("code"):   # design D22: planning only
        spec["compat_decision_id"] = compat_id
    if done[step["of"]].subjects.get("code"):   # ruling R9: the code lenses; design D24: a spec bundle's own
        spec["lenses"] = list(SPEC_LENSES if done[step["of"]].subjects.get("spec") else CODE_LENSES)
    if not bound:
        if half:  # a half-finished prepare: the same key finishes an identical spec, otherwise it is re-keyed
            stale = half[-1]
            return StepState(sid, "review", "ready", None,
                             {"kind": "review-prepare", "spec": {**spec, "key": stale["idempotency_key"]},
                              "finishes": stale["id"]}, {**subjects, "assignment_id": stale["id"]})
        return StepState(sid, "review", "ready", None, {"kind": "review-prepare", "spec": spec}, subjects)
    if half:
        return StepState(sid, "review", "ready", None, {"kind": "cancel-stale", "assignment_id": half[-1]["id"]},
                         {**subjects, "assignment_id": half[-1]["id"]})
    head = bound[-1]
    subjects = {**subjects, "assignment_id": head["id"]}
    state = head["state"]
    if state == "prepared":
        if head["owner_generation"] != facts["generation"]:
            return StepState(sid, "review", "ready", None, {"kind": "cancel-stale", "assignment_id": head["id"]},
                             subjects)
        return StepState(sid, "review", "ready", None, {"kind": "launch", "assignment_id": head["id"],
                                                        "role": "reviewer"}, subjects)
    if state in HOLDING_STATES:
        return StepState(sid, "review", "in-flight", "attempt-unknown", None, {**subjects, "holder": head["id"]})
    if state == "resolved-unknown":
        return StepState(sid, "review", "needs-human", "attempt-resolved", None, subjects)
    ret = json.loads(head["return_json"]) if head["return_json"] else {}
    if state not in TERMINAL_STATES or ret.get("provenance_class") != "observed":
        return StepState(sid, "review", "needs-human", "unobserved-return", None, {**subjects, "holder": head["id"]})
    if state != "completed-claim":
        return StepState(sid, "review", "needs-human", "reviewer-blocked", None, subjects)
    return StepState(sid, "review", "ready", None, {"kind": "review-record", "assignment_id": head["id"]}, subjects)


def _pointers(finding_json, severities) -> list[str]:
    try:
        view = json.loads(finding_json)["finding"]
        return [f"/{name}/{index}" for name in ("findings", "dispositions")
                for index, item in enumerate(view[name]) if item["severity"] in severities]
    except (KeyError, TypeError, ValueError):
        return ["/"]


def open_high_findings(finding_json) -> list[str]:
    """JSON pointers (`/findings/<i>`, `/dispositions/<i>`) into a recorded review's finding view for every item rated
    `high`: review findings carry no disposition, so each one stays open, and a carried item disposed at `high` is
    open too. An unreadable record is reported as `/` (fails safe)."""
    return _pointers(finding_json, ("high",))


def blocking_findings(finding_json, accept_on_high: bool) -> list[str]:
    """The pointers of every item that blocks acceptance, in view order: each one rated `critical`, plus each one
    rated `high` unless the pipeline accepts on high (correction-round design D2). `/` for an unreadable record."""
    return _pointers(finding_json, ("critical",) if accept_on_high else ("critical", "high"))


def corrections_used(snapshot: dict, facts: dict, feature_id: str, workspace: str, revision: int) -> int:
    """The correction attempts of an author step (design D1): its bound planning attempts at the mandate revision,
    not cancelled, whose binding continues a recorded review. Every prepared correction counts, whatever its
    outcome."""
    return sum(1 for row in snapshot["assignments"]
               if row["feature_id"] == feature_id and row["state"] != "cancelled-before-launch" and
               row["mandate_revision"] == revision and
               facts["bindings"].get(row["id"], {}).get("workspace") == workspace and
               (facts["bindings"][row["id"]].get("continues") or {}).get("corrects_review"))


def _accept_state(step, snapshot, facts, done, value) -> StepState:
    """Satisfied when an acceptance of the bundle exists and verify reports it current; a non-current one needs a
    human and is never redone. An open critical finding on the bundle needs a human once the correction budget is
    spent (design D6; the author step prepares corrections while it lasts), and so do open `high` findings of the
    review unless the pipeline sets `accept_on_high`; the stop carries `corrections: {used, max}`. A spec bundle
    (design D24, ruling P6) is the human's to accept unless the pipeline sets `spec_auto_accept`: `spec-decisions`
    without questions, with the digest and the clauses of the spec the human approves (or sends back with a revise
    decision, ruling P13). These three stops carry `finding_counts`, the counts of the review they name (design D26).
    Otherwise ready to accept the review. A story acceptance that is not current only because of an unrecorded commit
    above its commit, while the done status has no record, stays satisfied (`status_pending`): the status step
    re-adopts it."""
    sid, review = step["id"], done[step["of"]]
    bundle_id, review_id = review.subjects["bundle_id"], review.subjects["review_id"]
    subjects = {"step_id": sid, "bundle_id": bundle_id, "review_id": review_id}
    accepted = sorted((row["revision"], row["id"]) for row in snapshot["acceptances"] if row["bundle_id"] == bundle_id)
    if accepted:
        acceptance_id = accepted[-1][1]
        current = facts["acceptances"].get(acceptance_id, "unknown")
        subjects = {**subjects, "acceptance_id": acceptance_id}
        if current == "current":
            return StepState(sid, "accept", "satisfied", None, None, subjects)
        if _status_pending(facts, acceptance_id):
            return StepState(sid, "accept", "satisfied", None, None, {**subjects, "status_pending": True})
        return StepState(sid, "accept", "needs-human", "acceptance-stale", None, {**subjects, "verify": current})
    bundle_row = next((item for item in snapshot["bundles"] if item["id"] == bundle_id), None)
    sources = facts["bundles"].get(bundle_id, {}).get("sources", [])
    bindings = [facts["bindings"].get(item, {}) for item in sources]
    workspace = next((item["workspace"] for item in bindings if item.get("workspace")), None)
    profiles = {item.get("profile") for item in bindings} & set(implementation.STORY_PROFILES)
    if bundle_row is not None and profiles:   # ruling P5: the corrections of the bundle's kind (spec or implement)
        rev = bundle_row["mandate_revision"]
        bound = [row for row in snapshot["assignments"] if row["feature_id"] == bundle_row["feature_id"] and
                 row["state"] != "cancelled-before-launch" and row["mandate_revision"] == rev and
                 facts["bindings"].get(row["id"], {}).get("profile") in profiles]
        used = _story_corrections(bound, facts)
    else:
        used = corrections_used(snapshot, facts, bundle_row["feature_id"], workspace, bundle_row["mandate_revision"]) \
            if bundle_row is not None and workspace is not None else 0
    subjects = {**subjects, "corrections": {"used": used, "max": value["budgets"].get("max_corrections", 0)}}

    def counted(review_ref):   # design D26: the counts of the review the stop names
        row = next((item for item in snapshot["reviews"] if item["id"] == review_ref), None)
        return finding_counts(row["finding_json"] if row else None)
    critical = sorted(row["id"] for row in snapshot["reviews"]
                      if row["bundle_id"] == bundle_id and row["critical_open"])
    if critical:
        named = review_id if review_id in critical else critical[0]
        return StepState(sid, "accept", "needs-human", "critical-finding", None,
                         {**subjects, "review_id": named, "finding_counts": counted(named)})
    if not value.get("accept_on_high", False):
        review_row = next((row for row in snapshot["reviews"] if row["id"] == review_id), None)
        high = open_high_findings(review_row["finding_json"] if review_row else None)
        if high:
            return StepState(sid, "accept", "needs-human", "open-high-findings", None,
                             {**subjects, "finding_ids": high, "finding_counts": counted(review_id)})
    if profiles == {implementation.SPEC_AUTHOR} and not value.get("spec_auto_accept", False):
        head = facts["bundles"][bundle_id]["commit"]["assignment_id"]
        spec = facts["story"]["specs"].get(head) or {}
        return StepState(sid, "accept", "needs-human", "spec-decisions", None, {
            **subjects, "feature_id": bundle_row["feature_id"], "mandate_revision": bundle_row["mandate_revision"],
            "assignment_id": head, "spec_path": facts["bindings"][head].get("spec_path"), "questions": [],
            "digest": spec.get("digest"), "clauses": spec.get("clauses"), "finding_counts": counted(review_id)})
    return StepState(sid, "accept", "ready", None, {"kind": "accept", "bundle_id": bundle_id, "review_id": review_id},
                     subjects)


def _status_pending(facts: dict, acceptance_id: str) -> bool:
    """A story's accepted bundle commit whose only verify reason is an unrecorded commit above it while the `done`
    status has no record: the commit of a status step killed before its seal. That status step re-adopts it (or
    refuses) before currency applies again: each accept step of a story pipeline (the spec chain's, design D24, and
    the code chain's) is wrapped by its own status step (`spec`, `done`)."""
    story = facts.get("story")
    if story is None or "done" in story["status"]:
        return False
    reasons = story["acceptance_gates"].get(acceptance_id) or []
    return bool(reasons) and all(reason.endswith(implementation.UNRECORDED_ABOVE) for reason in reasons)


def _feature_subjects(snapshot: dict, feature_id: str) -> set:
    found = {feature_id}
    for table in ("assignments", "decisions", "decision_requests"):
        found |= {row["id"] for row in snapshot[table] if row["feature_id"] == feature_id}
    mine = {row["id"] for row in snapshot["bundles"] if row["feature_id"] == feature_id}
    found |= mine
    found |= {row["id"] for row in snapshot["reviews"] if row["bundle_id"] in mine}
    found |= {row["id"] for row in snapshot["acceptances"] if row["bundle_id"] in mine}
    return found


def last_domain_revision(snapshot: dict, feature_id: str) -> int:
    """The revision of the feature's last domain event (design section 5): driver events (`drive.*`, `pipeline.bound`,
    `judgment.*`, `inbox.rejected`, `story.*`) and `owner.*` events never make an export stale."""
    subjects = _feature_subjects(snapshot, feature_id)
    return max((row["revision"] for row in snapshot["events"] if row["subject_id"] in subjects and
                row["kind"] not in DRIVE_EVENTS and not row["kind"].startswith("owner.")), default=0)


def _export_state(step, snapshot, facts, feature) -> StepState:
    """Satisfied when the export pointer is at or after the feature's last domain event and its checkpoint directory
    exists under `to`; otherwise ready to publish a new revision directory (published ones are never overwritten)."""
    sid, info = step["id"], facts["export"] or {}
    subjects = {"step_id": sid, "to": step["to"]}
    last = last_domain_revision(snapshot, feature["id"])
    if info.get("revision") is not None and info["revision"] >= last and info["present"].get(step["to"]):
        return StepState(sid, "export", "satisfied", None, None,
                         {**subjects, "export_revision": info["revision"], "directory": info["dir_name"]})
    return StepState(sid, "export", "ready", None, {"kind": "export", "to": step["to"]},
                     {**subjects, "last_domain_revision": last})


def _story_corrections(bound, facts) -> int:
    return sum(1 for row in bound if any((facts["bindings"][row["id"]].get("continues") or {}).get(key)
                                         for key in ("corrects_review", "corrects_verify")))


def _story_updates(bound, facts) -> int:
    return sum(1 for row in bound if facts["bindings"][row["id"]].get("intent") == "update" and
               not any((facts["bindings"][row["id"]].get("continues") or {}).get(key)
                       for key in ("corrects_review", "corrects_verify")))


def _start(facts) -> dict:
    """Design D24: an epic story starts on its spec status commit (the spec chain moved HEAD past the base commit),
    so its baseline and its start status commit sit there while that commit is on the branch; `{}` for any other
    story."""
    sha = (facts["story"]["status"].get("spec") or {}).get("sha")
    return {"start": sha} if sha and facts["story"].get("spec_on_branch") else {}


def _setup_state(step, facts, feature) -> StepState:
    sid, story = step["id"], facts["story"]
    subjects = {"step_id": sid}
    setup, baseline = story["setup"], story["baseline"]
    start = _start(facts)
    fine = (setup and setup.get("outcome") == "pass" and setup.get("base_commit") == feature["base_commit"] and
            baseline and baseline.get("outcome") == "pass" and
            baseline.get("commit_sha") == start.get("start", feature["base_commit"]))
    if fine:
        return StepState(sid, "setup", "satisfied", None, None, {**subjects, "setup_ref": setup["ref"],
                                                                 "baseline_ref": baseline["ref"]})
    previous = {"setup": setup and setup.get("outcome"), "baseline": baseline and baseline.get("outcome")}
    return StepState(sid, "setup", "ready", None, {"kind": "setup", **start}, {**subjects, "previous": previous})


def _status_state(step, facts, done) -> StepState:
    sid, phase = step["id"], step["phase"]
    record = facts["story"]["status"].get(phase)
    subjects = {"step_id": sid, "phase": phase}
    if record and record.get("sha"):
        return StepState(sid, "status", "satisfied", None, None, {**subjects, "status_sha": record["sha"]})
    action = {"kind": "status", "phase": phase, **(_start(facts) if phase == "start" else {})}
    if phase in ("spec", "done"):
        accept = done[step["of"]].subjects
        action.update(acceptance_id=accept["acceptance_id"], bundle_id=accept["bundle_id"],
                      review_id=accept["review_id"])
    if phase == "spec":   # design D24 (ruling R3): the status commit sits on the accepted spec commit
        commit = facts["bundles"][accept["bundle_id"]]["commit"]
        action.update(assignment_id=commit["assignment_id"], commit_sha=commit["sha"])
    return StepState(sid, "status", "ready", None, action, subjects)


def _implement_state(step, sha, snapshot, facts, feature, value, kind="implement") -> StepState:
    """The head of an implement step, or of a spec step (`kind` "spec", design D24): its bound attempts are those of
    the kind's profile (ruling P5), so each kind keeps its own chain, updates and corrections."""
    sid, rev = step["id"], feature["mandate_revision"]
    subjects = {"step_id": sid}
    profile = implementation.SPEC_AUTHOR if kind == "spec" else implementation.IMPLEMENTATION
    prepared = {row["subject_id"]: row["revision"] for row in snapshot["events"]
                if row["kind"] == "assignment.prepared"}
    rows = [row for row in snapshot["assignments"] if row["feature_id"] == feature["id"]]
    live = [row for row in rows if row["state"] != "cancelled-before-launch" and row["mandate_revision"] == rev]
    bound = sorted((row for row in live if facts["bindings"].get(row["id"], {}).get("profile") == profile),
                   key=lambda row: prepared[row["id"]])
    prefix = f"drv-{sha[:12]}-{sid}-"
    key = _next_key(sha, sid, rows)
    half = sorted((row for row in live if row["id"] in facts["unbound"] and row["state"] == "prepared" and
                   _own(row["idempotency_key"], prefix)), key=lambda row: prepared[row["id"]])
    state = _implement_head(step, sid, snapshot, facts, bound, key, value, subjects, feature, sha, kind)
    if not half:
        return state
    stale = half[-1]
    if state.state == "ready" and state.action["kind"] == "prepare":
        spec = {**state.action["spec"], "key": stale["idempotency_key"]}
        return StepState(sid, kind, "ready", None, {**state.action, "spec": spec, "finishes": stale["id"]},
                         {**state.subjects, "assignment_id": stale["id"]})
    return StepState(sid, kind, "ready", None, {"kind": "cancel-stale", "assignment_id": stale["id"]},
                     {**subjects, "assignment_id": stale["id"]})


def _implement_head(step, sid, snapshot, facts, bound, key, value, subjects, feature, sha,
                    kind="implement") -> StepState:
    """Design D24: a spec attempt (`kind` "spec") has no verify; its completed commit is corrected after a recorded
    review as code is, an empty one is final, and an open request may ask several questions (ruling R1) whose
    refused judgment stops `spec-decisions`. Design D25: a client-stopped attempt is committed, never verified, bundled
    or reviewed, and continued by an update with `continues_stop` (counted in `max_updates`)."""
    client = value["roles"]["worker"]["client"]
    author = kind == "spec"
    # design D21 (F3): every story prepare carries the chain's resolving decisions, so the brief, the binding, the
    # bundle manifest and the code review brief see a decision that resolved an earlier attempt's question
    decisions = [{"id": item["id"]} for item in resolving_decisions(snapshot, {row["id"] for row in bound})]

    def spec(**extra):   # an operator's revise decision joins the update's decisions (ruling P13)
        return {"feature_id": feature["id"], "key": key, "author_id": step["author_id"], "client": client,
                "project": value["project"],
                "decisions": decisions + ([{"id": extra["revises"]}] if "revises" in extra else []),
                **({"web": True} if value["roles"]["worker"].get("web") is True else {}),
                **({"kind": "spec"} if author else {}), **extra}

    def prepare(intent, **extra):
        return StepState(sid, kind, "ready", None,
                         {"kind": "prepare", "profile": implementation.SPEC_AUTHOR if author else
                          implementation.IMPLEMENTATION, "intent": intent, "spec": spec(intent=intent, **extra)},
                         subjects)

    if not bound:
        return prepare("create")
    head = bound[-1]
    subjects = {**subjects, "assignment_id": head["id"]}
    state = head["state"]
    if state == "prepared":
        if head["owner_generation"] != facts["generation"]:
            return StepState(sid, kind, "ready", None, {"kind": "cancel-stale", "assignment_id": head["id"]},
                             subjects)
        return StepState(sid, kind, "ready", None,
                         {"kind": "launch", "assignment_id": head["id"], "role": "worker"}, subjects)
    if state in HOLDING_STATES:
        return StepState(sid, kind, "in-flight", "attempt-unknown", None, {**subjects, "holder": head["id"]})
    if state == "resolved-unknown":
        return StepState(sid, kind, "needs-human", "attempt-resolved", None, subjects)
    ret = json.loads(head["return_json"]) if head["return_json"] else {}
    if state == "client-stopped" and ret.get("provenance_class") == "observed":   # design D25
        commit = facts["story"]["commits"].get(head["id"])
        if commit is None:   # design D9: committed before anything else is derived
            return StepState(sid, kind, "ready", None, {"kind": "commit", "assignment_id": head["id"]}, subjects)
        subjects = {**subjects, "commit_sha": commit.get("sha"), "empty": bool(commit.get("empty")),
                    "client_stop": ret.get("client_stop")}
        if _story_updates(bound, facts) >= value["budgets"]["max_updates"]:
            return StepState(sid, kind, "needs-human", "update-budget", None, subjects)
        return prepare("update", update_of=head["id"], continues_stop=True)
    payload = facts["payloads"].get(head["id"])
    if state not in TERMINAL_STATES or ret.get("provenance_class") != "observed" or not isinstance(payload, dict):
        return StepState(sid, kind, "needs-human", "unobserved-return", None, {**subjects, "holder": head["id"]})
    commit = facts["story"]["commits"].get(head["id"])
    if commit is None:  # design D9: every terminal attempt is committed before anything else is derived
        return StepState(sid, kind, "ready", None, {"kind": "commit", "assignment_id": head["id"]}, subjects)
    subjects = {**subjects, "commit_sha": commit.get("sha"), "empty": bool(commit.get("empty"))}
    binding = facts["bindings"][head["id"]]
    continues = binding.get("continues") or {}
    budgets = value["budgets"]
    if state == "blocked":
        return StepState(sid, kind, "needs-human", "worker-blocked", None,
                         {**subjects, "condition": "returned blocked"})
    if state == "completed-claim":
        if commit.get("empty"):
            if continues.get("empty_attempt") or author:
                return StepState(sid, kind, "needs-human", "worker-blocked", None,
                                 {**subjects, "condition": "no change produced"})
            if _story_updates(bound, facts) >= budgets["max_updates"]:
                return StepState(sid, kind, "needs-human", "update-budget", None, subjects)
            return prepare("update", update_of=head["id"], empty_attempt=True)
        if not author:   # a spec attempt runs no verification (design D24)
            verify = facts["story"]["verifies"].get(head["id"])
            if verify is None:
                return StepState(sid, kind, "ready", None, {"kind": "verify", "assignment_id": head["id"]}, subjects)
            subjects = {**subjects, "verify_ref": verify["ref"], "verify_outcome": verify["outcome"]}
            if verify["outcome"] != "pass":
                if _story_corrections(bound, facts) >= budgets.get("max_corrections", 0):
                    return StepState(sid, kind, "satisfied", None, None, subjects)   # the verify step stops
                return prepare("update", update_of=head["id"], corrects_verify=verify["ref"])
        correction = _code_correction(sid, snapshot, facts, bound, head, commit, value, subjects, feature, prepare) or \
            (author and _spec_revision(sid, snapshot, facts, bound, head, commit, value, subjects, prepare))
        return correction or StepState(sid, kind, "satisfied", None, None, subjects)
    requests = [row for row in snapshot["decision_requests"] if row["assignment_id"] == head["id"]]
    if not requests:
        return StepState(sid, kind, "needs-human", "unobserved-return", None, subjects)
    request = requests[0]
    subjects["request_id"] = request["id"]
    if request["state"] == "open":
        if payload.get("conflicts"):
            return StepState(sid, kind, "needs-human", "question-conflict", None, subjects)
        if not author and len(payload.get("questions") or []) > 1:   # ruling R6: re-asked without a model
            return StepState(sid, kind, "ready", None, {"kind": "decide-form", "request_id": request["id"]},
                             subjects)
        stop = {"step_id": sid, "assignment_id": head["id"], "request_id": request["id"],
                "spec_path": binding.get("spec_path"), "questions": payload.get("open_questions") or [],
                "digest": (facts["story"]["specs"].get(head["id"]) or {}).get("digest")} if author else None
        return _judgment_state(sid, sha, request, facts, value, subjects, kind, stop)
    resolver = next((row for row in snapshot["decisions"] if row["id"] == request["resolution_decision_id"]), None)
    if resolver is None or resolver["mandate_revision"] != head["mandate_revision"]:
        return StepState(sid, kind, "needs-human", "stale-input", None,
                         {**subjects, "detail": "request resolved at another mandate revision"})
    if _story_updates(bound, facts) >= budgets["max_updates"]:
        return StepState(sid, kind, "needs-human", "update-budget", None, subjects)
    return prepare("update", update_of=head["id"], continues_request=request["id"])


def _open_bundle(snapshot, facts, head, commit) -> str | None:
    """The newest bundle of the head's commit at its revision, unless it is accepted."""
    found = sorted((item["revision"], bundle_id) for bundle_id, item in facts["bundles"].items()
                   if item["mandate_revision"] == head["mandate_revision"] and head["id"] in item["sources"] and
                   (item.get("commit") or {}).get("sha") == commit["sha"])
    if not found or any(row["bundle_id"] == found[-1][1] for row in snapshot["acceptances"]):
        return None
    return found[-1][1]


def _spec_revision(sid, snapshot, facts, bound, head, commit, value, subjects, prepare) -> StepState | None:
    """Ruling P13: the operator's `spec-revise:<bundle>` decision (`{"revise": <text>}`, operator-reported, at the
    head's revision) for the newest unaccepted bundle of the head's commit prepares a spec-author update carrying it,
    counted against max_updates; any other decision is ignored."""
    bundle_id = _open_bundle(snapshot, facts, head, commit)
    decision = bundle_id and next((row for row in snapshot["decisions"] if row["feature_id"] == head["feature_id"] and
                                   row["mandate_revision"] == head["mandate_revision"] and
                                   row["source_ref"] == implementation.REVISE + bundle_id and
                                   implementation.revise_text(row) is not None), None)
    if not decision:
        return None
    subjects = {**subjects, "bundle_id": bundle_id, "decision_id": decision["id"]}
    if _story_updates(bound, facts) >= value["budgets"]["max_updates"]:
        return StepState(sid, "spec", "needs-human", "update-budget", None, subjects)
    return StepState(sid, "spec", "ready", None,
                     prepare("update", update_of=head["id"], revises=decision["id"]).action, subjects)


def _code_correction(sid, snapshot, facts, bound, head, commit, value, subjects, feature, prepare) -> StepState | None:
    """The correction-round trigger for a code bundle: the newest bundle of the head's commit has an observed review
    with blocking findings, no acceptance, and the story's corrections are below max_corrections."""
    budget = value["budgets"].get("max_corrections", 0)
    if budget <= 0:
        return None
    bundle_id = _open_bundle(snapshot, facts, head, commit)
    if bundle_id is None:
        return None
    recorded = {row["subject_id"]: row["revision"] for row in snapshot["events"] if row["kind"] == "review.recorded"}
    observed = sorted((recorded.get(row["id"], 0), row["id"], row["finding_json"]) for row in snapshot["reviews"]
                      if row["bundle_id"] == bundle_id and row["provenance_class"] == "observed")
    if not observed:
        return None
    _, review_id, finding_json = observed[-1]
    blocking = blocking_findings(finding_json, value.get("accept_on_high", False))
    if not blocking or "/" in blocking or _story_corrections(bound, facts) >= budget:
        return None
    state = prepare("update", update_of=head["id"], corrects_review=review_id, finding_ids=blocking)
    return StepState(sid, state.kind, "ready", None, state.action,
                     {**subjects, "bundle_id": bundle_id, "review_id": review_id, "finding_ids": blocking})


def _commit_state(step, facts, done) -> StepState:
    sid, head = step["id"], done[step["of"]].subjects["assignment_id"]
    commit = facts["story"]["commits"].get(head)
    subjects = {"step_id": sid, "assignment_id": head}
    if commit is None:
        return StepState(sid, "commit", "ready", None, {"kind": "commit", "assignment_id": head}, subjects)
    return StepState(sid, "commit", "satisfied", None, None, {**subjects, "commit_sha": commit.get("sha"),
                                                              "empty": bool(commit.get("empty")),
                                                              "commit_ref": commit["ref"]})


def _verify_state(step, facts, done, value, snapshot, feature) -> StepState:
    sid, commit = step["id"], done[step["of"]].subjects
    head = commit["assignment_id"]
    subjects = {"step_id": sid, "assignment_id": head, "commit_sha": commit["commit_sha"]}
    verify = facts["story"]["verifies"].get(head)
    if verify is None:
        return StepState(sid, "verify", "ready", None, {"kind": "verify", "assignment_id": head}, subjects)
    subjects = {**subjects, "verify_ref": verify["ref"], "outcome": verify["outcome"]}
    if verify["outcome"] == "pass":
        return StepState(sid, "verify", "satisfied", None, None, subjects)
    rev = feature["mandate_revision"]
    bound = [row for row in snapshot["assignments"] if row["feature_id"] == feature["id"] and
             row["state"] != "cancelled-before-launch" and row["mandate_revision"] == rev and
             facts["bindings"].get(row["id"], {}).get("profile") == implementation.IMPLEMENTATION]   # ruling P5
    used = {"used": _story_corrections(bound, facts), "max": value["budgets"].get("max_corrections", 0)}
    return StepState(sid, "verify", "needs-human", "correction-budget", None, {
        **subjects, "corrections": used,
        "failed": [item["name"] for item in verify.get("results", []) if item["outcome"] not in ("pass", "skipped")]})


def _spec_chain(steps) -> set:
    """The ids of the spec step and of its bundle, review, accept and `status spec` steps (design D24)."""
    found = {step["id"]: step for step in steps}
    return {sid for sid, step in found.items() if step["kind"] == "spec" or
            (step["kind"] == "status" and step["phase"] == "spec") or
            (step["kind"] in ("bundle", "review", "accept") and _head(sid, found)["kind"] == "spec")}


def _spec_receipt(step, receipt: dict, feature: dict) -> StepState:
    """Ruling R5: a spec-chain step satisfied by the sealed `spec` status receipt whose commit is on the branch, with
    the receipt's subjects; its status step is the `carry` (ruling R4) while the feature is still provisional."""
    result = receipt.get("result") or {}
    subjects = {"step_id": step["id"], **{key: result.get(key) for key in (
        "assignment_id", "commit_sha", "bundle_id", "review_id", "acceptance_id")}, "status_sha": receipt["sha"]}
    if step["kind"] == "status" and json.loads(feature["constraints_json"]).get("provisional") is True:
        return StepState(step["id"], "status", "ready", None, {
            "kind": "carry", "status_sha": receipt["sha"], "bundle_id": result.get("bundle_id"),
            "acceptance_id": result.get("acceptance_id")}, subjects)
    return StepState(step["id"], step["kind"], "satisfied", None, None, subjects)


def derive(value: dict, pipeline_sha256: str, snapshot: dict, facts: dict) -> list[StepState]:
    """Pure: the state of every step from the snapshot rows and the gathered facts. Steps after the first
    unsatisfied one are pending. An author step's head is the newest non-cancelled bound planning assignment with
    the step's skill and workspace at the current revision, ordered by its prepared-event revision; wrapper steps
    match their bundle, review and acceptance by binding content the same way. Ruling R5: once the `spec` status
    receipt's commit is on the branch, the spec chain is satisfied by that receipt, never derived again; a receipt
    off the branch stops the spec step `worktree-drift`."""
    feature = next(row for row in snapshot["features"] if row["id"] == value["feature_id"])
    story = facts.get("story") or {}
    written = (story.get("status") or {}).get("spec")
    receipt = written if story.get("spec_on_branch") else None
    chain = _spec_chain(value["steps"]) if receipt else set()
    states, compat_id, blocked, done = [], None, False, {}
    for step in value["steps"]:
        if blocked:
            states.append(StepState(step["id"], step["kind"], "pending"))
            continue
        kind = step["kind"]
        if feature["mandate_revision"] != value["mandate_revision"]:
            state = StepState(step["id"], kind, "needs-human", "mandate-revised", None, {"step_id": step["id"]})
        elif step["id"] in chain:
            state = _spec_receipt(step, receipt, feature)
        elif kind == "spec" and written:
            state = StepState(step["id"], kind, "needs-human", "worktree-drift", None, {
                "step_id": step["id"], "expected": written["sha"],
                "detail": f"worktree-drift: the spec status commit {written['sha'][:12]} is not on the story branch"})
        elif kind == "compat":
            state = _compat(step, snapshot, facts, feature)
            compat_id = state.subjects.get("compat_decision_id")
        elif kind == "author":
            accepted = done[step["prd_from"]].subjects["acceptance_id"] if "prd_from" in step else None
            state = _author_state(step, pipeline_sha256, snapshot, facts, feature, compat_id, value, accepted)
        elif kind == "bundle":
            state = _bundle_state(step, snapshot, facts, feature, done)
        elif kind == "review":
            state = _review_state(step, pipeline_sha256, snapshot, facts, feature, compat_id, value, done)
        elif kind == "accept":
            state = _accept_state(step, snapshot, facts, done, value)
        elif kind == "setup":
            state = _setup_state(step, facts, feature)
        elif kind == "status":
            state = _status_state(step, facts, done)
        elif kind in ("spec", "implement"):
            state = _implement_state(step, pipeline_sha256, snapshot, facts, feature, value, kind)
        elif kind == "commit":
            state = _commit_state(step, facts, done)
        elif kind == "verify":
            state = _verify_state(step, facts, done, value, snapshot, feature)
        else:
            state = _export_state(step, snapshot, facts, feature)
        states.append(state)
        done[step["id"]] = state
        blocked = state.state != "satisfied"
    return states


def carried_from(snapshot: dict, value: dict) -> str | None:
    """Ruling R4: the spec-ready commit of this pipeline's own carry when a driver stopped after its revise (the
    feature one revision on and no longer provisional, the carry's decision at the pipeline's revision), else None:
    the run finishes that carry instead of stopping `mandate-revised`."""
    feature = next(row for row in snapshot["features"] if row["id"] == value["feature_id"])
    if feature["mandate_revision"] != value["mandate_revision"] + 1 or \
       "provisional" in json.loads(feature["constraints_json"]) or \
       all(step["kind"] != "spec" for step in value["steps"]):
        return None
    found = [row for row in snapshot["decisions"] if row["feature_id"] == feature["id"] and
             row["mandate_revision"] == value["mandate_revision"] and row["source_ref"].startswith(SPEC_READY)]
    return json.loads(found[-1]["choice_json"])["spec_ready"] if found else None


def carry(repo, store, owner: OwnerRef, lease: dict, value: dict, feature: dict, status_sha: str, hook=None) -> dict:
    """Design D24 (ruling R4): after the `spec` status receipt, (a) a coordinator decision `spec-ready:<sha>` at the
    pipeline's revision, (b) the mandate revised from the spec at that commit (`spec_mandate`), (c) the pipeline
    carried to the new revision, sealed and bound, (d) the lease re-sealed with the carried digest (`carried_from`
    the old one) and anchored as a second `drive.leased` of the lease id. Returns the new lease. Idempotent after a
    kill: a recorded decision is reused, a feature already one revision on whose mandate is exactly the spec's (M5) is
    not revised again, an identical binding is a no-op and an identical lease file is anchored as it is. The patch is
    read at the status commit and must hold the frozen section of the accepted spec commit the receipt names (M5).
    `hook` (a test seam) runs at `after-carry-revise`."""
    from .drive import _seal   # the driver's sealed-file writer (drive imports this module)
    hook = hook or (lambda point, **info: None)
    coordinator, revision = Coordinator(store), value["mandate_revision"]
    snapshot = store.read_export_snapshot(owner)
    receipt = implementation.story_facts(store.path.parent, snapshot, feature["id"])["status"].get("spec") or {}
    if receipt.get("sha") != status_sha:
        raise DomainError("STALE_INPUT", "The carry's status commit is not the spec status receipt's")
    patch = implementation.spec_mandate(repo, feature, status_sha, (receipt.get("result") or {}).get("commit_sha"))
    if feature["mandate_revision"] == revision and json.loads(feature["constraints_json"]).get("provisional") is True:
        source = SPEC_READY + status_sha
        decision = next((row["id"] for row in snapshot["decisions"]
                         if row["feature_id"] == feature["id"] and row["mandate_revision"] == revision and
                         row["source_ref"] == source), None)
        if decision is None:
            decision = coordinator.record_decision(owner, DecisionInput(
                feature["id"], revision, "coordinator", source,
                "the accepted spec becomes the story's mandate (design D24)", {"spec_ready": status_sha}))
        coordinator.revise_mandate(owner, feature["id"], revision, patch, decision)
        hook("after-carry-revise")
    elif carried_from(snapshot, value) != status_sha or \
            {key: json.loads(feature[f"{key}_json"]) for key in patch} != patch:
        raise DomainError("STALE_INPUT", "The feature's mandate moved other than by this pipeline's carry")
    bound = bind(repo, store, owner, {**value, "mandate_revision": revision + 1})
    if lease["pipeline_sha256"] == bound["sha256"]:   # the newest lease names the carried pipeline already
        return lease
    carried = {**lease, "pipeline_sha256": bound["sha256"], "pipeline_ref": bound["ref"],
               "carried_from": lease["pipeline_sha256"]}
    base = store.path.parent
    path = base / "drives" / lease["lease_id"] / f"lease-r{revision + 1}.json"
    try:
        ref, digest = _seal(base, path, carried)
    except FileExistsError:   # sealed before a kill, never anchored: the same bytes are anchored now
        ref, digest = path.relative_to(base).as_posix(), hashlib.sha256(canonical(carried)).hexdigest()
        _read_sealed(base, ref, digest)
    coordinator.anchor_event(owner, "drive.leased", lease["lease_id"], (feature["id"], ref, digest))
    return carried
