"""JSON CLI for the local coordinator state and one-attempt worker."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from . import boundary
from . import bill, bmad_contract, bmad_planning, bmad_review, bmad_snapshot, clients, drive, drive_epic, drive_pipeline
from . import inbox
from .exporter import export_checkpoint, import_checkpoint
from .installation import doctor
from .model import (AssignmentInput, BundleInput, DecisionInput, DomainError, FeatureInput,
                    OwnerRef, ReturnEvidence, ReviewEvidence)
from .repo import resolve_repo
from .run_receipt import canonical, private_file
from .service import Coordinator, holds_fence
from .store import open_store
from .worker import inspect_run, reconcile_run, resolution_evidence, run_assignment


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DomainError("INVALID_INPUT", "Invalid command arguments")


CLIENT_HELP = ("worker client: codex-cli (default), claude-code or copilot (authors; judgments and spec or code "
               "reviews in read mode); must match a bound "
               "assignment's client")
TURNS_HELP = "claude-code only (default 60); refused for codex-cli and copilot"
BOUNDARY_HELP = ("run the client in a bubblewrap sandbox (launch-boundary design); a binding that names a boundary "
                 "uses it without the flag, and a flag cannot change it")
DRIVE_DEFAULTS = {"max_turns": 20, "deadline_seconds": 7200.0, "human_wait_seconds": 0.0}
RESOLVE_COST = ("The fence is released on operator evidence only: an unobserved descendant of this attempt may still "
                "write, and the files it left stay unclaimed and are never bundled as observed work. That cost is the "
                "operator's recorded responsibility.")
_LOCKS = []  # shared drive-lock descriptors held for the whole duration of an owner-fenced command


@dataclass(frozen=True)
class _Exit:
    value: dict
    code: int


def _parser():
    parser = Parser(description="Local coordinator state and one-attempt worker")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def leaf(name, *, parent=commands, token=False, stdin=False):
        item = parent.add_parser(name)
        if token:
            item.add_argument("--token-file", type=Path, required=True)
        if stdin:
            item.set_defaults(stdin=True)
        return item

    leaf("init")
    leaf("status").add_argument("--all", action="store_true", help="also list closed features")
    diagnosed = leaf("doctor")
    diagnosed.add_argument("--base", required=True)
    diagnosed.add_argument("--client", help="also preflight this worker client (absolute binary and --version only)")
    diagnosed.add_argument("--boundary", choices=boundary.KINDS,
                           help="also check the launch boundary (a no-op sandbox, and with --client the client's "
                                "--version inside its mount set); never a model call")
    diagnosed.add_argument("--bmad", action="store_true",
                           help="also check the BMAD install for the planning path (tracked, or gitignored and "
                                "ready to materialize); never runs installed code")
    diagnosed.add_argument("--client-root", default=".agents/skills")
    worker = leaf("worker").add_subparsers(dest="action", required=True, parser_class=Parser)
    inspected = leaf("inspect", parent=worker)
    inspected.add_argument("id")
    launched = leaf("run", parent=worker, token=True)
    launched.add_argument("id")
    launched.add_argument("--model", required=True)
    launched.add_argument("--timeout", type=int, required=True)
    launched.add_argument("--client", help=CLIENT_HELP)
    launched.add_argument("--max-turns", type=int, help=TURNS_HELP)
    launched.add_argument("--boundary", choices=boundary.KINDS, help=BOUNDARY_HELP)
    reconciled = leaf("reconcile", parent=worker, token=True)
    reconciled.add_argument("id")
    bmad = leaf("bmad").add_subparsers(dest="action", required=True, parser_class=Parser)
    discovered = leaf("discover", parent=bmad)
    discovered.add_argument("--client-root", default=".agents/skills")
    materialized = leaf("materialize", parent=bmad, token=True)   # BMAD snapshot design S10
    materialized.add_argument("--client-root", default=".agents/skills")
    materialized.add_argument("--tree", help="an earlier snapshot's id (64 hex), not the main worktree's install")
    leaf("prepare", parent=bmad, token=True, stdin=True)
    bmad_run = leaf("run", parent=bmad, token=True)
    bmad_run.add_argument("id")
    bmad_run.add_argument("--model", required=True)
    bmad_run.add_argument("--timeout", type=int, required=True)
    bmad_run.add_argument("--client", help=CLIENT_HELP)
    bmad_run.add_argument("--max-turns", type=int, help=TURNS_HELP)
    bmad_run.add_argument("--boundary", choices=boundary.KINDS, help=BOUNDARY_HELP)
    bmad_status = leaf("status", parent=bmad)
    bmad_status.add_argument("id")
    leaf("bundle", parent=bmad, token=True, stdin=True)
    bmad_review = leaf("review", parent=bmad).add_subparsers(dest="step", required=True, parser_class=Parser)
    leaf("prepare", parent=bmad_review, token=True, stdin=True)
    reviewed = leaf("record", parent=bmad_review, token=True)
    reviewed.add_argument("id")
    leaf("accept", parent=bmad, token=True, stdin=True)
    leaf("verify", parent=bmad)
    detail = leaf("detail")
    detail.add_argument("kind")
    detail.add_argument("id")
    claim = leaf("claim")
    claim.add_argument("--session", required=True)
    claim.add_argument("--token-file", type=Path, required=True)
    leaf("release", token=True)
    leaf("recover")
    leaf("verify")
    feature = leaf("feature").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=feature, token=True, stdin=True)
    revise = leaf("revise", parent=feature, token=True, stdin=True)
    revise.add_argument("id")
    leaf("close", parent=feature, token=True, stdin=True).add_argument("id")   # feature lifecycle design F2
    decision = leaf("decision").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=decision, token=True, stdin=True)
    assignment = leaf("assignment").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=assignment, token=True, stdin=True)
    for name in ("cancel-prepared", "reserve-launch"):
        item = leaf(name, parent=assignment, token=True)
        item.add_argument("id")
        if name == "reserve-launch":
            item.add_argument("--client", help=CLIENT_HELP)
    retry = leaf("retry", parent=assignment, stdin=True)
    retry.add_argument("id")
    returned = leaf("return", parent=assignment, token=True, stdin=True)
    returned.add_argument("id")
    bundle = leaf("bundle").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("capture", parent=bundle, token=True, stdin=True)
    review = leaf("review").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=review, token=True, stdin=True)
    leaf("accept", token=True, stdin=True)
    exported = leaf("export", token=True)
    exported.add_argument("--to", type=Path, required=True)
    imported = leaf("import", stdin=True)
    imported.add_argument("--from", dest="source", type=Path, required=True)
    driven = leaf("drive").add_subparsers(dest="action", required=True, parser_class=Parser)
    started = leaf("start", parent=driven, token=True)
    started.add_argument("--feature", required=True)
    started.add_argument("--pipeline", type=Path, required=True)
    resumed = leaf("resume", parent=driven, token=True)
    for item in (started, resumed):
        item.add_argument("--max-turns", type=int)
        item.add_argument("--deadline", type=float)
        item.add_argument("--human-wait", type=float)
        item.add_argument("--judgment-root", type=Path)
    leaf("status", parent=driven).add_argument("--feature")
    box = leaf("inbox").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=box).add_argument("type", choices=("decision", "stop"))
    leaf("list", parent=box).add_argument("--feature")
    attempt = leaf("attempt").add_subparsers(dest="action", required=True, parser_class=Parser)
    resolved = leaf("resolve", parent=attempt, token=True)
    resolved.add_argument("id")
    resolved.add_argument("--confirm-unknown", action="store_true")
    story = leaf("story").add_subparsers(dest="action", required=True, parser_class=Parser)
    leaf("add", parent=story, token=True, stdin=True)
    epic = leaf("epic").add_subparsers(dest="action", required=True, parser_class=Parser)
    epic_start = leaf("start", parent=epic, token=True, stdin=True)
    epic_start.add_argument("--pipeline", type=Path, required=True,
                            help="a story pipeline with __FEATURE__ and __KEY__")
    epic_resume = leaf("resume", parent=epic, token=True)
    epic_resume.add_argument("--epic", help="the epic to continue after a release (with a token file to create)")
    epic_resume.add_argument("--skip", help="the story the last stop named, left to the operator")
    epic_resume.add_argument("--restart", help="the story the last stop named, after a terminal stop: a new mandate "
                                               "revision, a fresh pipeline with corrections at zero, the next attempt "
                                               "(design E15)")
    epic_resume.add_argument("--roles", type=Path, help="with --restart: a JSON file {role: {timeout?, max_turns?}} "
                                                        "that becomes the story's roles override (design E14)")
    epic_resume.add_argument("--budgets", type=Path, help="with --restart: a JSON file {\"max_cost_usd\": N} that "
                                                          "becomes the story's budgets override (bill design B5)")
    epic_resume.add_argument("--max-cost-usd", type=float, help="raises the epic's own cost bound before the run "
                                                                "(bill design B6)")
    leaf("status", parent=epic).add_argument("--epic")
    billed = leaf("bill")   # bill design B3: read only, no token, no lock
    billed.add_argument("--epic", help="every story of the epic, in order, and its total")
    billed.add_argument("--feature", help="one story or feature")
    billed.add_argument("--lease", help="one lease")
    for item in (epic_start, epic_resume):
        item.add_argument("--max-turns", type=int)
        item.add_argument("--deadline", type=float)
        item.add_argument("--human-wait", type=float)
        item.add_argument("--judgment-root", type=Path)
    return parser


def _object():
    try:
        value = json.load(sys.stdin)
    except (ValueError, UnicodeError) as exc:
        raise DomainError("INVALID_INPUT", "Input must be one JSON object") from exc
    if not isinstance(value, dict):
        raise DomainError("INVALID_INPUT", "Input must be one JSON object")
    return value


def _shape(value, required, optional=()):
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise DomainError("INVALID_INPUT", "Input fields do not match the command schema")
    return value


def _record(cls, value):
    required = [field.name for field in fields(cls) if field.default is MISSING and field.default_factory is MISSING]
    optional = [field.name for field in fields(cls) if field.name not in required]
    value = _shape(value, required, optional)
    hints = get_type_hints(cls)
    if any(not _matches(item, hints[key]) for key, item in value.items()):
        raise DomainError("INVALID_INPUT", "Input field has the wrong type")
    return cls(**value)


def _matches(value, expected):
    origin = get_origin(expected)
    if expected is Any:
        return True
    if origin in (Union, UnionType):
        return any(_matches(value, member) for member in get_args(expected))
    if origin is list:
        return isinstance(value, list) and all(_matches(item, get_args(expected)[0]) for item in value)
    if origin is dict:
        key_type, value_type = get_args(expected)
        return isinstance(value, dict) and all(_matches(key, key_type) and _matches(item, value_type)
                                                for key, item in value.items())
    return type(value) is expected


def _owner(path, repo=None):
    """The token's OwnerRef. With repo (every owner-fenced command), first take the shared drive lock and hold it until
    the command ends, so a fenced command and a driver never act concurrently (driver design D3)."""
    if repo is not None:
        _LOCKS.append(drive.acquire_lock(repo, exclusive=False))
    path = path.absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise DomainError("UNSAFE_PATH", "Token file path is unsafe")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise DomainError("UNSAFE_PATH", "Token file must be private and regular")
            data = json.load(stream)
    except (OSError, ValueError, UnicodeError) as exc:
        raise DomainError("UNSAFE_PATH", "Cannot read private token file") from exc
    if not isinstance(data, dict) or set(data) != {"token", "generation"} or \
       not isinstance(data["token"], str) or type(data["generation"]) is not int:
        raise DomainError("INVALID_INPUT", "Token file is malformed")
    return OwnerRef(data["token"], data["generation"])


def _client_readiness(name: str) -> dict:
    """Doctor's client check: the same preflight as a launch, so no model call and no attempt."""
    chosen = clients.get(name)
    found = shutil.which(chosen.executable, path=clients.static_env(chosen).get("PATH", os.defpath))
    notes = clients.doctor_notes(chosen)  # Copilot token names and warning; empty for other clients
    try:
        binary, version = clients.preflight(chosen)
    except DomainError as exc:
        return {"name": chosen.name, "binary": os.path.abspath(found) if found else None, "version": None,
                "ready": False, "reasons": [exc.message], **notes}
    return {"name": chosen.name, "binary": binary, "version": version, "ready": True, "reasons": [], **notes}


def _boundary_readiness(kind: str, name: str | None, repo) -> dict:
    """Launch-boundary L7 in doctor: the same checks as a boundary launch, without the run directory."""
    env = clients.static_env(clients.get(name)) if name else dict(os.environ)
    try:
        binary, version = boundary.preflight(env)
        if name is not None:
            clients.boundary_ready(kind, clients.get(name), repo)
    except DomainError as exc:
        found = shutil.which("bwrap", path=env.get("PATH", os.defpath))
        return {"kind": kind, "binary": os.path.abspath(found) if found else None, "version": None, "ready": False,
                "reasons": [exc.message]}
    return {"kind": kind, "binary": binary, "version": version, "ready": True, "reasons": []}


def _pipeline(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DomainError("INVALID_INPUT", "Pipeline file must be one readable JSON object") from exc
    if not isinstance(value, dict):
        raise DomainError("INVALID_INPUT", "Pipeline file must be one readable JSON object")
    return value


def _roles_file(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DomainError("INVALID_INPUT", "Roles file must be one readable JSON object") from exc
    if not isinstance(value, dict):
        raise DomainError("INVALID_INPUT", "Roles file must be one readable JSON object")
    return value


def _drive(repo, args):
    bounds = {"max_turns": args.max_turns, "deadline_seconds": args.deadline, "human_wait_seconds": args.human_wait}
    bounds = {key: value for key, value in bounds.items() if value is not None}
    if args.action == "start":
        summary, code = drive.start(repo, args.feature, _pipeline(args.pipeline), args.token_file,
                                    drive.RunBounds(**{**DRIVE_DEFAULTS, **bounds}), args.judgment_root)
    else:  # exempt from the shared lock: resume takes only the exclusive one
        summary, code = drive.resume(repo, _owner(args.token_file), bounds or None, args.judgment_root)
    return _Exit(summary, code)


def _epic(repo, args):
    bounds = {"max_turns": args.max_turns, "deadline_seconds": args.deadline, "human_wait_seconds": args.human_wait}
    bounds = {key: value for key, value in bounds.items() if value is not None}
    if args.action == "start":
        summary, code = drive_epic.start(repo, _record(drive_epic.EpicInput, _object()), _pipeline(args.pipeline),
                                         args.token_file, drive.RunBounds(**{**DRIVE_DEFAULTS, **bounds}),
                                         args.judgment_root)
    else:  # exempt from the shared lock: resume takes only the exclusive one; no token file yet is E12's re-claim
        if args.roles is not None and args.restart is None:   # the pairing before the roles file is read
            raise DomainError("INVALID_INPUT", "--roles goes with --restart")
        if args.budgets is not None and args.restart is None:
            raise DomainError("INVALID_INPUT", "--budgets goes with --restart")
        owner = _owner(args.token_file) if args.token_file.exists() else None
        summary, code = drive_epic.resume(repo, owner, bounds or None, args.judgment_root, skip=args.skip,
                                          restart=args.restart,
                                          roles=_roles_file(args.roles) if args.roles is not None else None,
                                          budgets=_roles_file(args.budgets) if args.budgets is not None else None,
                                          max_cost_usd=args.max_cost_usd,
                                          epic_id=args.epic, token_path=args.token_file)
    return _Exit(summary, code)


def _attempt_resolve(repo, store, args):
    """Operator only (D15): refuse unless every liveness check passes, seal the evidence, then resolve_unknown_attempt
    in one owner-fenced transaction. The driver never calls this."""
    if not args.confirm_unknown:
        raise DomainError("INVALID_INPUT", "attempt resolve requires --confirm-unknown")
    data = _shape(_object(), ("reason", "source_ref"))
    if any(type(value) is not str or not value.strip() for value in data.values()):
        raise DomainError("INVALID_INPUT", "Reason and source_ref must be non-blank text")
    owner = _owner(args.token_file, repo)
    row = next((item for item in store.read_export_snapshot(owner)["assignments"] if item["id"] == args.id), None)
    if row is None:
        raise DomainError("NOT_FOUND", "Assignment does not exist")
    if not holds_fence(row["state"], row["return_json"]):
        raise DomainError("BLOCKED_EVIDENCE", "Only an attempt that holds the fence can be resolved")
    evidence = resolution_evidence(repo, store, args.id)
    if evidence["spawn"] == "never" and set(drive.lock_state(repo)["pids"]) - {os.getpid()}:
        # a concurrent fenced `worker run` may sit between its reservation and its run-directory mkdir
        raise DomainError("BLOCKED_EVIDENCE", "Another fenced command holds the shared lock; a never-spawned "
                          "reservation may be about to launch, retry when it ends")
    if evidence["spawn"] == "unrecorded":
        raise DomainError("BLOCKED_EVIDENCE", "The run directory has no launch intent: the pid was never recorded")
    if evidence["pid_state"] == "alive":
        raise DomainError("BLOCKED_EVIDENCE", f"The recorded process {evidence['pid']} is alive "
                          "(same pid, start time and boot)")
    if evidence["cmdline_hits"]:
        raise DomainError("BLOCKED_EVIDENCE", "A /proc cmdline contains the launch token: pids " +
                          ", ".join(str(item["pid"]) for item in evidence["cmdline_hits"]))
    record = canonical({"kind": "attempt-resolution", "format": 1, **evidence, "generation": owner.generation,
                        **data, "resolved_at": datetime.now(timezone.utc).isoformat()})
    digest = hashlib.sha256(record).hexdigest()
    directory = store.path.parent / "resolutions"
    if directory.is_symlink():
        raise DomainError("UNSAFE_PATH", "Resolution directory is a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        private_file(directory / f"{args.id}-{digest}.json", record)
    except FileExistsError:
        pass
    ref = f"resolutions/{args.id}-{digest}.json"
    decision = Coordinator(store).resolve_unknown_attempt(owner, args.id, ref, digest, data["reason"],
                                                          data["source_ref"])
    return {"assignment_id": args.id, "state": "resolved-unknown", "decision_id": decision, "evidence_ref": ref,
            "evidence_sha256": digest, "spawn": evidence["spawn"], "pid": evidence["pid"],
            "pid_state": evidence["pid_state"], "cost": RESOLVE_COST}


def _run(args):
    repo = resolve_repo(args.repo)
    if args.command == "drive" and args.action == "status":
        return drive.status(repo, args.feature)
    if args.command == "epic" and args.action == "status":   # read only, before the store opens (epic-drive E13)
        return drive_epic.status(repo, args.epic)
    if args.command == "bill":   # read only, before the store opens (bill design B3)
        return bill.command(repo, epic=args.epic, feature=args.feature, lease=args.lease)
    if args.command == "inbox":
        if args.action == "add":
            return inbox.add(repo, args.type, _object())
        try:
            return {"messages": inbox.listing(repo, open_store(repo), args.feature)}
        except DomainError as exc:
            if exc.code == "UNINITIALIZED":
                return {"state": "uninitialized"}
            raise
    if args.command == "doctor":
        result = asdict(doctor(repo, args.base))
        if args.client is not None:
            result["client"] = _client_readiness(args.client)
            if not result["client"]["ready"]:
                result["ready"] = False
                result["reasons"] = result["reasons"] + [f"Worker client {args.client} is not ready"]
        if args.boundary is not None:
            result["boundary"] = _boundary_readiness(args.boundary, args.client, repo)
            if not result["boundary"]["ready"]:
                result["ready"] = False
                result["reasons"] = result["reasons"] + [f"Boundary {args.boundary} is not ready"]
        if args.bmad:   # BMAD snapshot design S5
            result["bmad"] = bmad_snapshot.doctor(repo, args.client_root)
            if result["bmad"]["reasons"]:
                result["ready"] = False
                result["reasons"] = result["reasons"] + ["The BMAD install is not ready for the planning path"]
        return result
    if args.command == "init":
        return {"repository_id": open_store(repo, create=True, exclusive=True).repository_id}
    if args.command == "bmad" and args.action == "discover":
        try:   # BMAD snapshot design S7: a drifted materialized install is compared before it is executed
            events = open_store(repo).read_snapshot()["events"]
        except DomainError as exc:
            if exc.code != "UNINITIALIZED":
                raise
            events = []
        bmad_snapshot.guard(repo.worktree, repo.common_dir, events)
        report = asdict(bmad_contract.discover(repo.worktree, args.client_root))
        if not report["supported"] and args.client_root in bmad_snapshot.ROOTS and \
           bmad_snapshot.classify(repo.worktree, args.client_root) == "absent" and \
           not any(os.path.lexists(repo.worktree / top) for top in ("_bmad", args.client_root)):
            report["reasons"].append("no BMAD install in this worktree: run bmad materialize")   # design S10
        return report
    if args.command == "import":
        data = _shape(_object(), (), ("worktree_map",))
        mapping = data.get("worktree_map", {})
        if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(path, str)
                                                for key, path in mapping.items()):
            raise DomainError("INVALID_INPUT", "Worktree map must contain paths by feature ID")
        return asdict(import_checkpoint(repo, args.source, {key: Path(path) for key, path in mapping.items()}))
    try:
        store = open_store(repo)
    except DomainError as exc:
        if exc.code == "UNINITIALIZED" and args.command in ("status", "detail", "recover", "verify") or \
           exc.code == "UNINITIALIZED" and args.command == "assignment" and args.action == "retry":
            return {"state": "uninitialized"}
        raise
    coordinator = Coordinator(store)
    if args.command == "drive":
        return _drive(repo, args)
    if args.command == "epic":
        return _epic(repo, args)
    if args.command == "attempt":
        return _attempt_resolve(repo, store, args)
    if args.command == "worker":
        if args.action == "inspect":
            return asdict(inspect_run(repo, store, args.id))
        owner = _owner(args.token_file, repo)
        if args.action == "run":
            return asdict(run_assignment(repo, store, owner, args.id, args.model, args.timeout, args.client,
                                         args.max_turns, args.boundary))
        return asdict(reconcile_run(repo, store, owner, args.id))
    if args.command == "bmad":
        if args.action == "status":
            return bmad_planning.view_return(repo, store, args.id)
        if args.action == "verify":
            return bmad_review.verify_bmad(store)
        owner = _owner(args.token_file, repo)
        if args.action == "materialize":
            return bmad_snapshot.materialize_command(repo, store, owner, args.client_root, args.tree)
        if args.action == "run":
            return bmad_planning.run(repo, store, owner, args.id, args.model, args.timeout, args.client,
                                     args.max_turns, args.boundary)
        if args.action == "bundle":
            return bmad_review.bundle_for_workspace(repo, store, owner, _record(bmad_review.BundleRequest, _object()))
        if args.action == "review":
            if args.step == "record":
                return bmad_review.record_bound_review(repo, store, owner, args.id)
            return bmad_review.review_prepare(repo, store, owner, _record(bmad_review.ReviewPrepare, _object()))
        if args.action == "accept":
            data = _shape(_object(), ("bundle_id", "review_id"))
            if type(data["bundle_id"]) is not str or type(data["review_id"]) is not str:
                raise DomainError("INVALID_INPUT", "Acceptance IDs must be text")
            accepted = bmad_review.accept_bound(store, owner, data["bundle_id"], data["review_id"])
            review = next(row for row in store.read_snapshot()["reviews"] if row["id"] == data["review_id"])
            # Critical-only rule unchanged; open high findings are listed (the driver stops on them by default).
            return {**accepted, "open_high_findings": drive_pipeline.open_high_findings(review["finding_json"])}
        return bmad_planning.prepare(repo, store, owner, _record(bmad_planning.PlanningPrepare, _object()))
    if args.command == "status":
        return coordinator.status(include_closed=args.all)
    if args.command == "detail":
        return coordinator.detail(args.kind, args.id)
    if args.command == "recover":
        return asdict(coordinator.recover_status())
    if args.command == "verify":
        return asdict(coordinator.verify())
    if args.command == "claim":
        owner = coordinator.claim(args.session, args.token_file)
        return {"generation": owner.generation}
    if args.command == "assignment" and args.action == "retry":
        data = _shape(_object(), ("request_fingerprint",))
        if type(data["request_fingerprint"]) is not str:
            raise DomainError("INVALID_INPUT", "Request fingerprint must be text")
        return asdict(coordinator.retry_assignment(args.id, data["request_fingerprint"]))
    data = _object() if getattr(args, "stdin", False) else None
    owner = _owner(args.token_file, repo)
    if args.command == "release":
        coordinator.release(owner)
        return {"ok": True}
    if args.command == "export":
        receipt = export_checkpoint(store, owner, args.to)
        return {"directory": str(receipt.directory), "revision": receipt.revision, "digest": receipt.digest}
    if args.command == "feature":
        if args.action == "add":
            return {"id": coordinator.create_feature(owner, _record(FeatureInput, data))}
        if args.action == "close":
            data = _shape(data, ("expected_revision", "decision_id", "resolution"))
            if type(data["expected_revision"]) is not int or type(data["decision_id"]) is not str or \
               type(data["resolution"]) is not str:
                raise DomainError("INVALID_INPUT", "Close input has the wrong type")
            return {"state": coordinator.close_feature(owner, args.id, **data)}
        data = _shape(data, ("expected_revision", "patch", "decision_id"))
        if type(data["expected_revision"]) is not int or not isinstance(data["patch"], dict) or \
           type(data["decision_id"]) is not str:
            raise DomainError("INVALID_INPUT", "Mandate revision input has the wrong type")
        return {"mandate_revision": coordinator.revise_mandate(owner, args.id, **data)}
    if args.command == "story":
        from . import bmad_implementation
        return bmad_implementation.story_add(repo, store, owner, _record(bmad_implementation.StoryInput, data))
    if args.command == "decision":
        return {"id": coordinator.record_decision(owner, _record(DecisionInput, data))}
    if args.command == "assignment":
        if args.action == "add":
            return {"id": coordinator.create_assignment(owner, _record(AssignmentInput, data))}
        if args.action == "return":
            coordinator.record_return(owner, args.id, _record(ReturnEvidence, data))
        elif args.action == "cancel-prepared":
            coordinator.cancel_prepared(owner, args.id)
        else:
            coordinator.reserve_launch(owner, args.id, args.client)
        return {"ok": True}
    if args.command == "bundle":
        return {"id": coordinator.capture_bundle(owner, _record(BundleInput, data))}
    if args.command == "review":
        return {"id": coordinator.record_review(owner, _record(ReviewEvidence, data))}
    data = _shape(data, ("bundle_id", "review_id"))
    if type(data["bundle_id"]) is not str or type(data["review_id"]) is not str:
        raise DomainError("INVALID_INPUT", "Acceptance IDs must be text")
    return {"id": coordinator.accept(owner, data["bundle_id"], data["review_id"])}


def main():
    try:
        result = _run(_parser().parse_args())
    except DomainError as exc:
        code = exc.code
        status = (3 if code == "OWNERSHIP_CONFLICT" else
                  5 if code in {"CORRUPT_STATE", "INCOMPATIBLE_SCHEMA", "INVALID_CHECKPOINT", "CHECKPOINT_CONFLICT"} else
                  4 if code in {"UNINITIALIZED", "UNKNOWN_EVIDENCE", "BLOCKED_EVIDENCE", "RECOVERY_BLOCKED", "STALE_INPUT",
                                "FEATURE_CLOSED",
                                "UNSUPPORTED_CONTRACT", "UNSUPPORTED_CLIENT_MODE", "UNSUPPORTED_STEP"} else 2)
        print(json.dumps({"error": code, "message": exc.message, **getattr(exc, "extra", {})}), file=sys.stderr)
        return status
    except (TypeError, ValueError, KeyError) as exc:
        print(json.dumps({"error": "INVALID_INPUT", "message": "Invalid command input"}), file=sys.stderr)
        return 2
    finally:
        while _LOCKS:
            os.close(_LOCKS.pop())
    if isinstance(result, _Exit):
        print(json.dumps(result.value, sort_keys=True, allow_nan=False))
        return result.code
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
