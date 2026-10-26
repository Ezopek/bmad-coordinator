"""One foreground worker-client attempt per prepared coordinator assignment."""

import hashlib
import json
import os
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import bmad_snapshot, boundary, bundles, clients, profiles
from .installation import doctor
from .model import DomainError, LaunchEvidence, OwnerRef, ReturnEvidence
from .repo import GIT_SAFE, RepoIdentity, resolve_repo
from .run_receipt import (ReceiptEvidenceSource, canonical, delta, inventory, private_file,
                          read_receipt, receipt_ref, run_name, run_root, seal, stamp_json)
from .service import (CONTINUATION_UNSUPPORTED, Coordinator, _scope_allows, binding_anchor, check_directory_alias,
                      scope_covers)
from .store import Store


@dataclass(frozen=True)
class RunReport:
    assignment_id: str
    state: str
    run_dir: str | None
    evidence_needs: list[str]


FIELDS = {"assignment_id", "request_fingerprint", "result", "changed_paths", "checks",
          "descendants", "unresolved_question", "options", "affected_ids"}
STATE_OF = {"completed": "completed-claim", "blocked": "blocked", "needs-decision": "needs-decision",
            "client-stopped": "client-stopped"}   # design D25: the coordinator's own terminal result of a client stop
RETURNED = tuple(STATE_OF.values())


def _row(snapshot, table, record_id):
    row = next((item for item in snapshot[table] if item["id"] == record_id), None)
    if row is None:
        raise DomainError("NOT_FOUND", f"{table[:-1]} does not exist")
    return row


def _current_file(worktree: Path, entry: dict) -> bool:
    try:
        path = worktree / entry["path"]
        return path.is_file() and not path.is_symlink() and \
            hashlib.sha256(path.read_bytes()).hexdigest() == entry["digest"]
    except (OSError, KeyError, TypeError):
        return False


def _check_inputs(worktree: Path, assignment: dict) -> None:
    sources = json.loads(assignment["source_digests_json"])
    contracts = json.loads(assignment["contract_files_json"])
    if any(not _current_file(worktree, entry) for entry in sources + contracts) or \
       hashlib.sha256(canonical(contracts)).hexdigest() != assignment["contract_digest"]:
        raise DomainError("STALE_INPUT", "Assignment source or contract bytes changed")


def _immutable_inputs(assignment: dict) -> list[dict]:
    allowed = json.loads(assignment["paths_json"])
    sources = json.loads(assignment["source_digests_json"])
    contracts = json.loads(assignment["contract_files_json"])
    return contracts + [entry for entry in sources if not any(scope_covers(scope, entry["path"]) for scope in allowed)]


def _check_checkout(repo: RepoIdentity, feature: dict) -> None:
    identity = resolve_repo(Path(feature["worktree"]))
    if identity != repo:
        raise DomainError("INVALID_REPOSITORY", "Recorded feature worktree or common directory differs")
    branch = subprocess.run(["git", *GIT_SAFE, "-C", str(repo.worktree), "symbolic-ref", "--quiet", "--short", "HEAD"],
                            capture_output=True, text=True)
    if branch.returncode != 0 or branch.stdout.strip() != feature["branch"]:
        raise DomainError("INVALID_REPOSITORY", "Feature branch differs or is detached")
    report = doctor(repo, feature["base_commit"])
    if not report.ready or report.base_commit != feature["base_commit"]:
        raise DomainError("STALE_INPUT", "Installed package or committed base is not ready: " + "; ".join(report.reasons))


READ_CWD = "read-cwd"   # Copilot reviewer design V3: the staged copy inside the run directory
READ_DROPPED = frozenset({"bundle_read"})   # Copilot reviewer design V6: a read-mode reviewer's reads are observed


def _extra(profile, read: bool) -> dict:
    return {key: value for key, value in profile.extra.items() if not (read and key in READ_DROPPED)}


def _schema(profile=profiles.GENERIC, read: bool = False) -> dict:
    return {"type": "object", "additionalProperties": False, "required": sorted(FIELDS | set(_extra(profile, read))),
            "properties": {
        "assignment_id": {"type": "string"}, "request_fingerprint": {"type": "string"},
        "result": {"enum": ["completed", "needs-decision", "blocked"]},
        "changed_paths": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
        "descendants": {"type": "array", "items": {"type": "string"}},
        "unresolved_question": {"type": ["string", "null"]},
        "options": {"type": "array", "items": {"type": "string"}},
        "affected_ids": {"type": "array", "items": {"type": "string"}},
        **_extra(profile, read),
    }}


def _final(value, assignment, profile=profiles.GENERIC, read: bool = False):
    if not isinstance(value, dict) or set(value) != FIELDS | set(_extra(profile, read)) or \
       value.get("assignment_id") != assignment["id"] or \
       value.get("request_fingerprint") != assignment["request_fingerprint"] or \
       value.get("result") not in ("completed", "needs-decision", "blocked") or \
       any(not isinstance(value.get(key), list) or any(type(item) is not str for item in value[key])
           for key in ("changed_paths", "checks", "descendants", "options", "affected_ids")) or \
       type(value.get("unresolved_question")) not in (str, type(None)) or \
       (value["result"] == "needs-decision" and not value["unresolved_question"]):
        raise DomainError("UNKNOWN_EVIDENCE", "Worker final JSON does not match required schema or assignment")
    return value


class InvalidReturn(Exception):
    """Design D27: a sealed return the profile refuses after the scope and integrity checks passed: the worker's own
    slip (shape, fields, a mismatch with the file it wrote), which a story continuation can fix."""


def _invalid_return_stop(exc: Exception) -> dict:
    return {"kind": "invalid-return", "reason": str(exc)[:1000]}


def _refusal(final_bytes: bytes, assignment, profile, binding, directory, changed) -> str | None:
    """Design D27 at replay: the reason the retained return is refused, recomputed; None when it stands."""
    try:
        profile.check(_final(json.loads(final_bytes), assignment, profile), assignment, binding, directory, changed)
    except DomainError as exc:
        return exc.message[:1000]
    except ValueError as exc:
        return str(exc)[:1000]
    return None


def _failure_stop(cause: str, process, signals: list, timeout_seconds: int) -> dict:
    """Design D29: the coordinator's own observation of a client that ended without a return: it killed the group
    at the role's time box, or the process exited non-zero without a client stop; the group is drained and the
    integrity record clean in either case, else the attempt stays unknown."""
    stop = {"kind": "client-failure", "cause": cause, "exit_code": process.poll(), "signals": list(signals)}
    if cause == "timeout":
        stop["timeout_seconds"] = timeout_seconds
    return stop


def _stop_final(assignment, stop: dict) -> dict:
    """Design D25: the coordinator's record of a client stop: no worker field, the stop as the client reported it."""
    return {"assignment_id": assignment["id"], "request_fingerprint": assignment["request_fingerprint"],
            "result": "client-stopped", "changed_paths": [], "checks": [], "descendants": [],
            "unresolved_question": None, "options": [], "affected_ids": [], "client_stop": stop}


_stop_group = clients.stop_group


def _observed_reads(final: dict, binding: dict, reads: dict) -> dict:
    """Copilot reviewer design V5, V6: a read-mode review's `bundle_read`, observed by the coordinator: every input the
    binding marks required read in full (a successful view returned its bound bytes), each sealed file by its manifest
    path and its digest. One not read in full refuses the return, as a `bundle_read` mismatch does."""
    if final["result"] != "completed":   # a blocked or undecided return is never a review, so nothing is attested
        return {**final, "bundle_read": []}
    for item in binding["read_inputs"]:
        if item["required"] and reads.get(item["alias"]) != item["digest"]:
            raise DomainError("UNKNOWN_EVIDENCE", f"The read-mode reviewer did not read {item['path']} in full")
    # §10 R3 (as F16 for the sealed location): a finding that locates by the staged alias names the manifest path
    aliases = {item["alias"]: item["path"] for item in binding["read_inputs"]}
    findings = [{**finding, "location": _unaliased(finding["location"], aliases)} if isinstance(finding, dict) and
                isinstance(finding.get("location"), str) else finding for finding in final.get("findings") or []]
    return {**final, "findings": findings, "bundle_read": [{"path": item["path"], "sha256": item["digest"]}
                                                           for item in binding["sealed_files"]]}


def _unaliased(location: str, aliases: dict) -> str:
    for alias, path in aliases.items():
        if location == alias or location.startswith(alias + ":"):
            return path + location[len(alias):]
    return location


def _read_refusal(directory: Path, launch: dict, assignment, binding, profile) -> str | None:
    """§10 R1: what the one repair round would be asked to fix: the answer is not one JSON object, it does not fit
    the schema, a required input was not read in full, or the review profile refuses it. None when the return
    stands, or when the stream itself is defective (nothing a second answer repairs)."""
    try:
        final_bytes, reads, error = clients.read_answer(directory, launch)
    except DomainError:
        return None
    if error:
        return error
    try:
        final = _observed_reads(_final(json.loads(final_bytes), assignment, profile, True), binding, reads)
        if final["result"] == "completed":
            profile.check(final, assignment, binding, directory, [])
    except DomainError as exc:
        return exc.message
    except (ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return None


READ_RETURN = ("Your cwd is `{cwd}` and holds only the staged inputs the brief lists under `inputs/`. Use only the "
               "view, grep and glob tools, on paths under `{cwd}/inputs/`: never the cwd itself, its .git or any "
               "other path; any other path voids the review. View every file marked `read in full` whole, once "
               "(pass forceReadLargeFiles for a large file). You have no tool to write: your final message is your "
               "return, exactly one JSON object. It starts with `{{` and ends with `}}`: no sentence, heading or note "
               "before or after it, or the review is void.")


# `affected_ids` as Coordinator.record_return enforces it, quoting its refusal; every envelope and every BMAD brief
# states it (smoke attempt 2: S3 returned requirement ids and ended `unknown`).
AFFECTED_IDS = ("`affected_ids` lists only coordinator assignment ids of this feature (this assignment's "
                "`assignment_id` or an assignment id named in the brief), never requirement, finding, question, "
                "decision or file ids; leave it `[]` unless your result affects such an assignment (refused: Affected "
                "ID must be an assignment in this feature).")
# The generic return contract as `_final` and `_return_evidence` enforce it, quoting their refusals.
_ROLE = ("Act only in the assigned role and mandate. Do not perform top-level intake or claim coordinator "
         "ownership. Return exactly the required JSON schema. Checks, permissions, and descendant status are "
         "claims until independently observed. Copy `assignment_id` and `request_fingerprint` from this "
         "envelope; `result` is `completed`, `needs-decision` with a non-empty `unresolved_question`, or "
         "`blocked`; ")
_IMMUTABLE = ("Never change the brief, a contract file, a frozen input outside your writable paths, "
              "`AGENTS.md`, `CLAUDE.md`, `.bmad-coordinator/`, `_bmad/`, the `.agents/` or `.claude/` skill trees, "
              "the Git common directory or any path outside the granted scopes (refused: Worker final JSON does not "
              "match required schema or assignment; Immutable source or contract bytes changed; Worker changed "
              "paths exceed scope or conflict with observed bytes; a change to sealed coordinator evidence also "
              "ends the attempt unknown). " + AFFECTED_IDS)
INSTRUCTIONS = (_ROLE + "`changed_paths` lists exactly the worktree-relative files that Git does not ignore and "
                "whose final bytes differ from the start (created or modified), each inside `writable_paths`, and "
                "is `[]` for a read-only role. Listing a Git-ignored file or a file restored to its original bytes, "
                "deleting, renaming or symlinking a file, or a mode-only change is refused and ends the attempt "
                "unknown. " + _IMMUTABLE)
# The story profiles' contract (design D8, D24): Git is the evidence of change, the worker's list a declaration.
_STORY_DELTA = ("`changed_paths` lists the worktree-relative files you created or modified inside `writable_paths`; it "
                "is your declaration, the coordinator takes the observed change from Git (the cumulative delta from "
                "the story's base commit) and a declaration that differs from it is recorded, not refused. ")
_STORY_REFUSED = ("Symlinking a file, a mode-only change, or touching a protected path or any path outside the granted "
                  "scopes is refused and ends the attempt unknown. ")
STORY_INSTRUCTIONS = (_ROLE + _STORY_DELTA + "Deleting or renaming a file inside `writable_paths` is allowed and "
                      "recorded. " + _STORY_REFUSED + _IMMUTABLE)
# Design D24: the spec author's check refuses a deleted or renamed spec.
SPEC_INSTRUCTIONS = (_ROLE + _STORY_DELTA + "Deleting or renaming the spec file is refused and ends the attempt "
                     "unknown. " + _STORY_REFUSED + _IMMUTABLE)


# Worker-file clients only (ruling i): the generic envelope names the exact return path, so an unbound author knows
# where its return goes; Codex and Claude envelopes carry no return_path and keep INSTRUCTIONS unchanged.
RETURN_FILE = ("Write the required JSON object, and nothing else, to `{}` (this envelope's `return_path`) and "
               "nowhere else, with your file-creation tool (for example `create`, or `edit` to fix it); a shell "
               "redirect is accepted but discouraged. Your final message is never read as the return.")
# Worker-file clients only (return-schema round, ruling 1): such a client has no schema flag (Copilot smoke 2: S1
# omitted `checks` and `descendants`), so the envelope lists every required key with its type and a skeleton, rendered
# from the same schema object the run writes to return.schema.json and `_final` and the profile check enforce.
RETURN_SCHEMA = "The return object has exactly these keys, all required: {0}. Skeleton (fill every value): `{1}`."
# The id fields' real shape (a generated uuid4 and a sha256 hex digest); an envelope fills in its own values.
ID_SHAPES = {"assignment_id": "<uuid4>", "request_fingerprint": "<sha256 hex>"}
# Script-launcher clients with bound commands only (design section 14, rulings R3, R3a and the return-schema round):
# the envelope prints every bound invocation by the launcher's bare name, the only form Copilot approves (smoke 2: an
# absolute-path call is matched as the full path and denied). Codex and Claude envelopes are unchanged.
SCRIPT_LAUNCHER = ("Run the bound BMAD scripts only through the coordinator's launcher, by its bare name `{0}` (it is "
                   "first on your PATH; a call by its full path is denied), never through `uv` directly: {1}, each "
                   "with your arguments only after the full printed form. The launcher refuses anything else and runs "
                   "nothing.")


def _schema_type(spec: dict) -> str:
    if "enum" in spec:
        return "one of " + ", ".join(json.dumps(item) for item in spec["enum"])
    kind = spec.get("type")
    if isinstance(kind, list):
        return " or ".join(_schema_type({"type": item}) for item in kind)
    if kind == "array":
        return "array of " + _schema_type(spec.get("items", {}))
    if kind == "object":
        return "object {" + ", ".join(f"`{key}`: {_schema_type(item)}"
                                      for key, item in sorted(spec.get("properties", {}).items())) + "}"
    return str(kind)


def _skeleton(spec: dict):
    if "enum" in spec:
        return "<" + "|".join(str(item) for item in spec["enum"]) + ">"
    kind = spec.get("type")
    if isinstance(kind, list):
        return None if "null" in kind else _skeleton({"type": kind[0]})
    if kind == "array":
        item = spec.get("items", {})
        return [_skeleton(item)] if item.get("type") == "object" else []
    if kind == "object":
        return {key: _skeleton(item) for key, item in sorted(spec.get("properties", {}).items())}
    return f"<{kind}>"


def return_schema_text(schema: dict, ids: dict | None = None) -> str:
    """Every required key of a return schema with its type, then a JSON skeleton, both from that one schema object.
    The id fields show their real shape, or the envelope's own values when `ids` gives them."""
    properties, filled = schema["properties"], {**ID_SHAPES, **(ids or {})}
    listing = "; ".join(f"`{key}`: {_schema_type(properties[key])}" for key in schema["required"])
    skeleton = json.dumps({key: filled[key] if key in filled else _skeleton(properties[key])
                           for key in schema["required"]}, ensure_ascii=False)
    return RETURN_SCHEMA.format(listing, skeleton)


def _story(profile_name) -> bool:
    """A story profile (design D8, D24); imported lazily, as the story profile module imports this one."""
    from .bmad_implementation import STORY_PROFILES
    return profile_name in STORY_PROFILES


def _envelope(assignment, feature, inventory_digest, model, timeout_seconds, return_path=None, schema=None, forms=(),
              hint=None, profile_name=None, read_cwd=None):
    from .bmad_implementation import SPEC_AUTHOR   # lazy: the story profile module imports this one
    instructions = INSTRUCTIONS if not _story(profile_name) else \
        SPEC_INSTRUCTIONS if profile_name == SPEC_AUTHOR else STORY_INSTRUCTIONS
    sources = json.loads(assignment["source_digests_json"])
    contracts = json.loads(assignment["contract_files_json"])
    value = {"role": "reviewer" if assignment["permission_mode"] == "read-only" else "worker",
            "assignment_id": assignment["id"], "request_fingerprint": assignment["request_fingerprint"],
            "mandate": {"outcome": feature["outcome"], "exclusions": json.loads(feature["exclusions_json"]),
                        "constraints": json.loads(feature["constraints_json"]),
                        "completion": json.loads(feature["completion_json"]),
                        "authority": json.loads(feature["authority_json"]),
                        "revision": feature["mandate_revision"]},
            "brief_ref": assignment["brief_ref"], "brief": (Path(feature["worktree"]) / assignment["brief_ref"]).read_text(encoding="utf-8"),
            "source_digests": sources, "contract_files": contracts,
            "contract_digest": assignment["contract_digest"],
            "writable_paths": json.loads(assignment["paths_json"]),
            "permission_mode": assignment["permission_mode"], "model": model,
            "timeout_seconds": timeout_seconds, "initial_inventory_digest": inventory_digest,
            "instructions": instructions}
    if return_path is not None:  # worker-file clients only, so Codex and Claude envelope bytes are unchanged
        value["return_path"] = return_path
        value["instructions"] = instructions + " " + RETURN_FILE.format(return_path) + " " + return_schema_text(
            schema, {"assignment_id": assignment["id"], "request_fingerprint": assignment["request_fingerprint"]})
        if hint:  # the profile's own return guidance (planning: the workflow_json skeleton)
            value["instructions"] += " " + hint
    if read_cwd is not None:   # Copilot reviewer design V4, V6: the read mode's rules and its final-message return
        value["instructions"] = instructions + " " + READ_RETURN.format(cwd=read_cwd) + " " + return_schema_text(
            schema, {"assignment_id": assignment["id"], "request_fingerprint": assignment["request_fingerprint"]})
    if forms:  # script-launcher clients with bound commands only
        value["instructions"] += " " + SCRIPT_LAUNCHER.format(clients.LAUNCHER_NAME, ", ".join(
            f"`{clients.LAUNCHER_NAME} {' '.join(form)} …`" for form in forms))
    return value


def _report(assignment_id, state, directory, needs=()):
    return RunReport(assignment_id, state, str(directory) if directory else None, list(needs))


def _return_evidence(repo, assignment, directory, before, after, final, thread, exit_code,
                     profile=profiles.GENERIC, binding=None, feature_scopes=(), client=None, extras=None):
    """Design D25: a client stop (`final` from `_stop_final`) passes the same scope and integrity checks as a return
    and is sealed with `client_stop` and no profile payload."""
    stopped = final["result"] == "client-stopped"
    changed = delta(before, after)
    allowed = json.loads(assignment["paths_json"])
    source_paths = {entry["path"] for entry in json.loads(assignment["source_digests_json"])}
    contract_paths = {entry["path"] for entry in json.loads(assignment["contract_files_json"])}
    protected = {assignment["brief_ref"]} | contract_paths | {"AGENTS.md"}
    protected |= {path for path in before if path.startswith(".bmad-coordinator/")}
    protected |= {path for path in source_paths if not any(scope_covers(scope, path) for scope in allowed)}
    immutable = _immutable_inputs(assignment)
    if any(not _current_file(repo.worktree, entry) for entry in immutable):
        raise DomainError("UNKNOWN_EVIDENCE", "Immutable source or contract bytes changed")
    if _story(profile.name) and binding is not None:
        # Design D8, ruling R2: Git is the evidence of change; the worker's list is a recorded claim. A touched
        # protected path (the spec, the status files, the package, the skill trees) is still refused, but for the
        # spec author's own spec path (design D24, ruling R6).
        from . import bmad_implementation as implementation
        touched = [path for path in changed if implementation.protected(path, binding.get("spec_path", ""),
                                                                         binding.get("project"))
                   and not (binding.get("role") == "spec-author" and path == binding["spec_path"])
                   or path in protected or not any(scope_covers(scope, path) for scope in allowed)
                   or not _scope_allows(path, feature_scopes)]
        found = implementation.git_delta(repo.worktree, binding["base_commit"], allowed)
        if (directory / "git-delta.json").exists():   # design D27: the stop's pass after a refused return
            if json.loads((directory / "git-delta.json").read_bytes()) != found:
                raise ValueError("git delta changed between the return and its stop")   # a coordinator fault, not a rule
        else:
            seal(directory / "git-delta.json", found)
        # Controller ruling (task 2): a symlink or other non-regular file, and a mode-only change, are refused.
        irregular = [path for path in changed if path in after and after[path].kind != "file"]
        mode_only = [path for path in changed if path in before and path in after and
                     before[path].digest == after[path].digest]
        changed = found["changed"]
        if touched or any(path not in after or after[path].kind != "file" for path in changed) or \
           (assignment["permission_mode"] == "read-only" and changed):
            raise DomainError("UNKNOWN_EVIDENCE", "Worker changed paths exceed scope or conflict with observed bytes")
        if irregular or mode_only:
            what = f"symlink or non-regular file {json.dumps(irregular[0])}" if irregular else \
                f"mode-only change {json.dumps(mode_only[0])}"
            raise DomainError("UNKNOWN_EVIDENCE", "Worker changed paths exceed scope or conflict with observed bytes: "
                              + what)
    elif changed != sorted(set(final["changed_paths"])) or len(final["changed_paths"]) != len(changed) or \
       any(not any(scope_covers(scope, path) for scope in allowed) or path in protected or path not in after or
           after[path].kind != "file" or not _scope_allows(path, feature_scopes) or
           (path in before and before[path].digest == after[path].digest) for path in changed) or \
       (assignment["permission_mode"] == "read-only" and changed):
        raise DomainError("UNKNOWN_EVIDENCE", "Worker changed paths exceed scope or conflict with observed bytes")
    refs = [{"path": path, "digest": after[path].digest} for path in changed]
    client = client or clients.get(None)
    source_id = clients.source_id(client.name, thread)
    value = {"kind": "return", "assignment_id": assignment["id"], "client": client.name,
             "request_fingerprint": assignment["request_fingerprint"],
             "worktree": str(repo.worktree), "common_dir": str(repo.common_dir),
             "source_id": source_id, "result": final["result"], "changed_paths": refs,
             "launch_sha256": hashlib.sha256((directory / "launch.json").read_bytes()).hexdigest(),
             "checks": final["checks"], "descendants": final["descendants"],
             "unresolved_question": final["unresolved_question"], "options": final["options"],
             "affected_ids": final["affected_ids"], "thread_id": thread, "exit_code": exit_code,
             "before": stamp_json(before), "after": stamp_json(after),
             "input_digests": json.loads(assignment["source_digests_json"]),
             "immutable_inputs": immutable, "claimed_paths": final["changed_paths"],
             "events_sha256": hashlib.sha256((directory / "events.jsonl").read_bytes()).hexdigest(),
             "last_sha256": hashlib.sha256((directory / "last.json").read_bytes()).hexdigest(),
             "ended_at": datetime.now(timezone.utc).isoformat(), **(extras or {})}
    if profile is not profiles.GENERIC:
        value["profile"] = profile.name
        try:
            value["profile_payload"] = None if stopped else profile.check(final, assignment, binding, directory,
                                                                           changed)
        except DomainError as exc:   # design D27: a profile refusal after the scope checks is the worker's slip
            if exc.code != "UNKNOWN_EVIDENCE" or not _story(profile.name):
                raise
            raise InvalidReturn(exc.message) from exc
    if stopped:
        value["client_stop"] = final["client_stop"]
    receipt_digest = seal(directory / "return.json", value)
    return ReturnEvidence(source_id, receipt_ref(directory.name, "return"), refs, final["checks"],
                          final["descendants"], final["result"], final["unresolved_question"],
                          final["options"], final["affected_ids"], receipt_digest, final.get("client_stop"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded_client(launch, value, binding, events, assignment_id):
    """One client per attempt: launch receipt (absent: Codex), reservation event (NULL: Codex), return receipt
    and binding must all name the same registered client."""
    name = launch.get("client", clients.DEFAULT)
    try:
        client = clients.get(name)
    except DomainError as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt names an unregistered worker client") from exc
    reserved = [row["source_id"] or clients.DEFAULT for row in events
                if row["subject_id"] == assignment_id and row["kind"] == "assignment.launch-reserved"]
    if reserved != [name] or value.get("client", clients.DEFAULT) != name or \
       (binding is not None and binding.get("client", clients.DEFAULT) != name):
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt client differs from the reservation, return or binding")
    return client


def _clean_integrity(directory: Path) -> None:
    try:
        fd = os.open(directory / "integrity.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("integrity record is not a private regular file")
            record = json.loads(stream.read())
    except (OSError, ValueError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed-evidence integrity record is missing") from exc
    if not clients.integrity_clean(record):
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed-evidence integrity record is not clean")


def _validated_receipt(repo, store, assignment, directory, historical=False):
    """Current-byte receipt validation; historical=True is internal and drops only whole-worktree equality
    and the immutable-input byte recheck (bound-review acceptance), never anchor/identity/envelope checks."""
    source = ReceiptEvidenceSource(repo, assignment["id"], assignment["request_fingerprint"], store, historical)
    events = store.read_snapshot()["events"]
    name, bound = binding_anchor(events, assignment["id"])
    profile = profiles.get(name)
    binding = source.binding() if name else None
    ref = receipt_ref(directory.name, "return")
    digest = source.terminal_digest()
    value = read_receipt(repo, ref, digest)
    launch_ref = receipt_ref(directory.name, "launch")
    launch = source._bound(launch_ref, value["launch_sha256"], "launch")
    source.classify_launch(LaunchEvidence(launch["source_id"], launch_ref,
                                          launch["observed_at"], launch["pid"], value["launch_sha256"]))
    if assignment["launch_ref"] not in (None, launch_ref):
        raise DomainError("UNKNOWN_EVIDENCE", "Stored launch reference differs")
    client = _recorded_client(launch, value, binding, events, assignment["id"])
    _check_boundary(launch, binding)
    if binding is not None and binding.get("bmad_source"):   # BMAD snapshot design S7: the stored snapshot
        bmad_snapshot.verify_store(repo.common_dir, binding["bmad_source"]["tree"])
    read = launch.get("requested_mode") == "read"   # Copilot reviewer design V1, V6
    if read != (binding is not None and binding.get("mode") == "read") or launch.get("qualification") is True:
        raise DomainError("UNKNOWN_EVIDENCE", "The receipt's read mode differs from its binding, or it is a "
                          "qualification run, which is never observed")
    repaired = (directory / clients.REPAIR_STREAM).exists()   # §10 R1: the repair round's retained bytes
    if repaired != ("repair_sha256" in value) or (repaired and (
            _sha256_file(directory / clients.REPAIR_RECORD) != value["repair_sha256"] or
            _sha256_file(directory / clients.REPAIR_STREAM) != value["repair_events_sha256"])):
        raise DomainError("UNKNOWN_EVIDENCE", "The repair round's retained bytes differ from the receipt")
    if "client" in launch:  # receipts written since the client registry also need a clean D16 record
        _clean_integrity(directory)
    if (launch.get("profile"), launch.get("binding_sha256"), value.get("profile")) != \
       (name, bound and bound["payload_digest"], name):
        raise DomainError("UNKNOWN_EVIDENCE", "Receipt profile differs from the anchored binding")
    if value.get("immutable_inputs") != _immutable_inputs(assignment) or \
       value.get("input_digests") != json.loads(assignment["source_digests_json"]):
        raise DomainError("UNKNOWN_EVIDENCE", "Immutable inputs no longer match receipt")
    if not historical:
        if any(not _current_file(repo.worktree, entry) for entry in value["immutable_inputs"]):
            raise DomainError("UNKNOWN_EVIDENCE", "Immutable inputs no longer match receipt")
        if stamp_json(inventory(repo.worktree)) != value.get("after"):
            raise DomainError("UNKNOWN_EVIDENCE", "Worktree bytes differ from terminal inventory")
    stopped = value.get("result") == "client-stopped"
    event = ReturnEvidence(value["source_id"], ref, value["changed_paths"], value["checks"],
                           value["descendants"], value["result"], value["unresolved_question"],
                           value["options"], value["affected_ids"], digest, value.get("client_stop"))
    source.classify_return(event)
    if client.observation_name is not None and \
       _sha256_file(directory / client.observation_name) != value.get("observation_sha256"):
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed client observation differs from the receipt")
    failed = stopped and (value.get("client_stop") or {}).get("kind") == "client-failure"   # design D29
    if client.return_source == "worker-file" and not read and not failed and \
       clients.worker_return_sha256(directory) != value.get("worker_return_sha256"):
        raise DomainError("UNKNOWN_EVIDENCE", "Retained worker return differs from the receipt")
    if failed:   # design D29: the stop is the coordinator's observation: the end record, the retained bytes
        stop = value["client_stop"]
        last = (directory / "last.json").read_bytes()
        try:
            end = json.loads((directory / "end.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise DomainError("UNKNOWN_EVIDENCE", "Failure stop has no end record") from exc
        expected = _stop_final(assignment, stop)
        if _sha256_file(directory / "events.jsonl") != value["events_sha256"] or \
           hashlib.sha256(last).hexdigest() != value["last_sha256"] or \
           stop.get("cause") not in ("timeout", "exit") or stop.get("exit_code") != end.get("exit_code") or \
           value.get("exit_code") != end.get("exit_code") or \
           (stop["cause"] == "timeout") != (end.get("timed_out") is True) or \
           (stop["cause"] == "exit" and end.get("exit_code") == 0) or \
           value.get("thread_id") != "process" or value.get("profile_payload") is not None or \
           not _story(profile.name) or value.get("claimed_paths") != [] or \
           any(value.get(key) != expected[key] for key in
               ("checks", "descendants", "unresolved_question", "options", "affected_ids")):
            raise DomainError("UNKNOWN_EVIDENCE", "Failure stop differs from the retained process evidence")
        return event
    if "script_launcher" in launch:  # the sealed post-run launcher check (fix round 2, I2)
        check = directory / clients.LAUNCHER_CHECK
        if _sha256_file(check) != value.get("launcher_check_sha256") or not clients.launcher_intact(
                json.loads(check.read_bytes()), launch["script_launcher"].get("sha256")):
            raise DomainError("UNKNOWN_EVIDENCE", "Sealed script launcher check differs or failed")
    terminal = client.terminal(directory, launch)  # the recorded client's parser, over retained bytes only
    if terminal.facts.get("answer_error"):
        raise DomainError("UNKNOWN_EVIDENCE", "Read-mode stream invalid: " + terminal.facts["answer_error"])
    thread = terminal.thread_id
    if value.get("thread_id") != thread or value.get("source_id") != clients.source_id(client.name, thread) or \
       clients.client_of(value["source_id"]) != client.name:
        raise DomainError("UNKNOWN_EVIDENCE", "Terminal thread differs from receipt")
    last = (directory / "last.json").read_bytes()
    if _sha256_file(directory / "events.jsonl") != value["events_sha256"] or \
       hashlib.sha256(last).hexdigest() != value["last_sha256"] or last != terminal.final or \
       (value["exit_code"] != 0 and not stopped):
        raise DomainError("UNKNOWN_EVIDENCE", "Terminal receipt no longer matches raw process output")
    if not clients.facts_agree(value.get("client_facts"), clients.client_facts(
            client, "read" if read else assignment["permission_mode"], terminal, web=launch.get("web") is True)):
        raise DomainError("UNKNOWN_EVIDENCE", "Client facts differ from the retained client output")
    if stopped:   # design D25: no final message, no profile payload; the stop and its fixed fields are re-derived
        stop = value.get("client_stop") or {}
        if stop.get("kind") == "invalid-return":   # design D27: the retained return, refused again for the same reason
            if terminal.stop is not None or last != terminal.final or read or stop.get("reason") != _refusal(
                    last, assignment, profile, binding, directory, [item["path"] for item in value["changed_paths"]]):
                raise DomainError("UNKNOWN_EVIDENCE", "Invalid-return stop differs from the retained client output")
            expected = _stop_final(assignment, stop)
        else:
            expected = _stop_final(assignment, terminal.stop)
            if terminal.stop is None or terminal.stop != stop or last != b"":
                raise DomainError("UNKNOWN_EVIDENCE", "Stop receipt differs from the retained client output")
        if value.get("profile_payload") is not None or not _story(profile.name) or value.get("claimed_paths") != [] or \
           any(value.get(key) != expected[key] for key in
               ("checks", "descendants", "unresolved_question", "options", "affected_ids")):
            raise DomainError("UNKNOWN_EVIDENCE", "Stop receipt differs from the retained client output")
        return event
    final = _final(json.loads(last), assignment, profile, read)
    if read:
        final = _observed_reads(final, binding, terminal.facts.get("reads", {}))
    if any(final[key] != value.get(key) for key in
           ("result", "checks", "descendants", "unresolved_question", "options", "affected_ids")) or \
       final["changed_paths"] != value.get("claimed_paths"):
        raise DomainError("UNKNOWN_EVIDENCE", "Final message differs from terminal receipt")
    if profile is not profiles.GENERIC and value.get("profile_payload") != profile.check(
            final, assignment, binding, directory, [item["path"] for item in value["changed_paths"]]):
        raise DomainError("UNKNOWN_EVIDENCE", "Profile payload differs from the checked final message")
    return event


def historical_return(repo: RepoIdentity, store: Store, assignment_id: str) -> ReturnEvidence:
    """Internal: validate an anchored observed return without later worktree equality."""
    assignment = _row(store.read_snapshot(), "assignments", assignment_id)
    if assignment["state"] not in RETURNED:
        raise DomainError("BLOCKED_EVIDENCE", "Assignment has no recorded return")
    event = _validated_receipt(repo, store, assignment, run_root(repo) / run_name(
        assignment_id, assignment["request_fingerprint"]), historical=True)
    stored = json.loads(assignment["return_json"])
    if stored["provenance_class"] != "observed" or stored["payload_digest"] != event.payload_digest or \
       stored["event_ref"] != event.event_ref:
        raise DomainError("UNKNOWN_EVIDENCE", "Stored return is not this observed receipt")
    return event


def _resolve_client(requested, binding):
    """Flag, else the binding's client, else the default; a flag that conflicts with the binding is refused."""
    bound = clients.bound_client(binding) if binding is not None else None
    chosen = clients.get(requested) if requested is not None else bound or clients.get(None)
    if bound is not None and chosen.name != bound.name:
        raise DomainError("INVALID_INPUT", "Worker client conflicts with the assignment binding")
    return chosen


def _resolve_boundary(requested, binding):
    """Launch-boundary L1: the flag, else the binding's boundary; a flag may add a boundary to a binding without
    one, never change or drop the one a binding names."""
    flag = boundary.check_kind(requested)
    bound = boundary.check_kind(binding.get("boundary")) if binding is not None else None
    if bound is not None and flag not in (None, bound):
        raise DomainError("INVALID_INPUT", "Boundary conflicts with the assignment binding")
    return flag or bound


def _check_boundary(launch: dict, binding) -> None:
    """Launch-boundary L5, at replay: a binding that names a boundary needs its record in the anchored launch
    receipt, and any record there must be well formed and end with the client argv."""
    kind = binding.get("boundary") if binding is not None else None
    record = launch.get("boundary")
    if kind is None and record is None:
        return
    problems = boundary.check_record(record, kind or (record.get("kind") if isinstance(record, dict) else None),
                                     launch.get("argv"))
    if problems:
        raise DomainError("UNKNOWN_EVIDENCE", problems[0])


def _integrity(repo, directory, baseline, group, git_before=None, state_before=None) -> dict | None:
    """D16: always written after the group drain and before any terminal classification; F24 adds the Git
    metadata comparison, before the coordinator's first Git call after the run. None when it cannot be written."""
    record = clients.integrity_record(repo, baseline, group, git_before, state_before)
    try:
        private_file(directory / "integrity.json", canonical(record))
    except OSError:
        return None
    return record


def run_assignment(repo: RepoIdentity, store: Store, owner: OwnerRef, assignment_id: str,
                   model: str, timeout_seconds: int, client: str | None = None,
                   max_turns: int | None = None, boundary_kind: str | None = None) -> RunReport:
    if type(model) is not str or not model.strip() or model.startswith("-") or type(timeout_seconds) is not int or \
       timeout_seconds <= 0:
        raise DomainError("INVALID_INPUT", "Worker model and positive timeout are required")
    if max_turns is not None and (type(max_turns) is not int or max_turns <= 0):
        raise DomainError("INVALID_INPUT", "Max turns must be a positive integer")
    snapshot = store.read_export_snapshot(owner)
    assignment = _row(snapshot, "assignments", assignment_id)
    feature = _row(snapshot, "features", assignment["feature_id"])
    if assignment["state"] != "prepared":
        raise DomainError("BLOCKED_EVIDENCE", "Assignment has already reserved its one attempt")
    if assignment["owner_generation"] != owner.generation or feature["mandate_revision"] != assignment["mandate_revision"]:
        raise DomainError("STALE_INPUT", "Assignment owner or mandate changed")
    mode = assignment["permission_mode"]
    paths = json.loads(assignment["paths_json"])
    if mode not in ("read-only", "workspace-write") or (mode == "read-only" and paths) or \
       (mode == "workspace-write" and not paths):
        raise DomainError("INVALID_INPUT", "Worker permission and writable paths are incompatible")
    protected = {assignment["brief_ref"], "AGENTS.md"} | \
        {entry["path"] for entry in json.loads(assignment["contract_files_json"])}
    if any(path.startswith(".bmad-coordinator/") or any(scope_covers(path, item) for item in protected)
           for path in paths):
        raise DomainError("UNSAFE_PATH", "Brief and installed contract must be outside writable paths")
    if any(not _scope_allows(path, json.loads(feature["writable_paths_json"])) for path in paths):
        raise DomainError("UNSAFE_PATH", "Assignment path is outside writable feature scopes")
    for path in paths:
        if path.endswith("/"):
            check_directory_alias(repo.worktree, path)
    profile_name, bound = binding_anchor(snapshot["events"], assignment_id)
    profile = profiles.get(profile_name)
    _check_checkout(repo, feature)
    binding = ReceiptEvidenceSource(repo, assignment_id, assignment["request_fingerprint"], store).binding() \
        if profile_name else None
    materialized = (binding or {}).get("bmad_source")   # BMAD snapshot design S7: a materialized install
    chosen = _resolve_client(client, binding)
    kind = _resolve_boundary(boundary_kind, binding)
    read = bool(binding) and binding.get("mode") == "read"   # Copilot reviewer design revision 6
    commands = clients.bound_commands(binding)
    if max_turns is not None and chosen.max_turns_default is None:
        raise DomainError("INVALID_INPUT", f"Worker client {chosen.name} does not take max turns")
    if read and (not chosen.read_mode or kind is None or mode != "read-only"):
        raise DomainError("UNSUPPORTED_CLIENT_MODE", f"Worker client {chosen.name} has no read mode outside the "
                          "launch boundary")
    if mode == "read-only" and not chosen.write_free_readonly and not read:
        raise DomainError("UNSUPPORTED_CLIENT_MODE", f"Worker client {chosen.name} has no write-free read-only mode")
    name = run_name(assignment_id, assignment["request_fingerprint"])
    root = run_root(repo)
    directory = root / name
    read_cwd = directory / READ_CWD if read else None   # V3: the staged copy, kept with the run's receipts
    return_path = str(directory / clients.WORKER_RETURN / "return.json") \
        if chosen.return_source == "worker-file" and not read else None
    # A bound author of a script-launcher client gets RUN/worker-tools/bmad-script; malformed commands, or an
    # interpreter or uv that cannot be bound (including an interpreter under the worktree or the common directory),
    # are refused here, before the reservation.
    launcher = clients.launcher_bytes(commands, clients.launcher_tools((repo.worktree, repo.common_dir)),
                                      str(repo.worktree)) if chosen.script_launcher and commands else None
    forms = clients.launcher_forms(commands) if launcher else ()
    schema = _schema(profile, read)  # one object: return.schema.json, the client argv and an envelope's listing
    hint = profile.hint(binding) if return_path and profile.hint and binding else None
    if chosen.max_envelope_bytes is not None and len(canonical(_envelope(
            assignment, feature, "0" * 64, model, timeout_seconds, return_path, schema, forms, hint, profile_name,
            str(read_cwd) if read else None))) > chosen.max_envelope_bytes:
        raise DomainError("INVALID_INPUT", "Envelope exceeds the worker client's envelope limit")
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise DomainError("UNSAFE_PATH", "Run directory is unsafe")
    if os.path.lexists(directory):
        raise DomainError("BLOCKED_EVIDENCE", "Run sidecar already exists; no relaunch")
    binary, version = clients.preflight(chosen)  # a missing or broken binary costs no attempt (D5)
    plan, qualified = None, True
    if read:   # V7, V8: the profile's qualification and every staged input, before the reservation
        qualified = clients.read_ready(chosen, version, binding["read_profile"])
        for item in binding["read_inputs"]:
            if hashlib.sha256(bundles.input_bytes(repo.worktree, item["source"])).hexdigest() != item["digest"]:
                raise DomainError("UNKNOWN_EVIDENCE", f"Read-mode input {item['path']} differs from its bound digest")
        plan = clients.plan_boundary(kind, chosen, binary, boundary.project_mounts("isolated", directory, read_cwd),
                                     clients.worktree_protected(repo))
    elif kind is not None:   # launch-boundary L4, L7: checked before the reservation, so a refusal costs nothing
        protected = clients.worktree_protected(repo)
        sealed = (binding["sealed_root"],) if binding and binding.get("sealed_root") else ()
        declared = ((binding or {}).get("project") or {}).get("boundary") or {}   # launch-boundary L4
        extra = [boundary.Mount(mode, os.path.normpath(os.path.expanduser(path)))
                 for mode in ("ro", "rw") for path in declared.get(mode, [])] if mode != "read-only" else []
        if materialized and mode != "read-only":   # BMAD snapshot design S8: the materialized roots, read-only
            extra += [boundary.Mount("ro", str(repo.worktree / top)) for top in ("_bmad", materialized["client_root"])]
        plan = clients.plan_boundary(kind, chosen, binary, boundary.project_mounts(
            "reviewer" if mode == "read-only" else "writer", directory, repo.worktree, repo.worktree, repo.common_dir,
            sealed=sealed, others=tuple(protected[3:])), protected, commands, extra)
    if materialized:   # BMAD snapshot design S7: the materialized install, compared before the reservation
        bmad_snapshot.guard(repo.worktree, repo.common_dir, store.read_snapshot()["events"], materialized["tree"])
    # The owner-fenced reservation is authoritative; ordinary unmet dependencies and fences
    # must fail before consuming an irreversible sidecar. The baseline snapshot follows it.
    Coordinator(store).reserve_launch(owner, assignment_id, chosen.name)
    _check_inputs(repo.worktree, assignment)
    before = inventory(repo.worktree)
    baseline = clients.sealed_evidence(repo, name)
    try:
        git_before = clients.git_metadata(repo)   # F24, taken with the D16 baseline
    except (OSError, ValueError, IndexError) as exc:
        raise DomainError("INVALID_REPOSITORY", "Cannot read the worktree's Git metadata") from exc
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DomainError("UNSAFE_PATH", "Run directory is unsafe after reservation")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DomainError("BLOCKED_EVIDENCE", "Run sidecar already exists; no relaunch") from exc
    for subdir in chosen.run_subdirs:
        (directory / subdir).mkdir(mode=0o700)
    staged, cwd_before = [], None
    if read:   # V2, V3: each input checked again and written once, in a Git-initialised directory of its own
        read_cwd.mkdir(mode=0o700)
        subprocess.run(["git", *GIT_SAFE, "init", "-q", str(read_cwd)], check=True, capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL)
        staged = bundles.stage_inputs(repo.worktree, read_cwd, binding["read_inputs"])
        cwd_before = inventory(read_cwd)
    if launcher is not None:
        clients.write_launcher(directory, launcher)
    envelope = canonical(_envelope(assignment, feature, hashlib.sha256(canonical(stamp_json(before))).hexdigest(),
                                   model, timeout_seconds, return_path, schema, forms, hint, profile_name,
                                   str(read_cwd) if read else None))
    private_file(directory / "envelope.json", envelope)
    private_file(directory / "return.schema.json", canonical(schema))
    if chosen.return_source == "client-file":
        private_file(directory / "last.json", b"")
    session = str(uuid.uuid4()) if chosen.presets_session else None
    spec = clients.LaunchSpec(binary, "read" if read else mode, read_cwd if read else repo.worktree, directory,
                              schema, model, max_turns if max_turns is not None else chosen.max_turns_default, session,
                              (binding["sealed_root"],) if binding and binding.get("sealed_root") and not read else (),
                              commands, web=bool(binding) and binding.get("web") is True and mode != "read-only",
                              return_path=return_path, isolated=read)
    cwd = read_cwd if read else repo.worktree
    argv = chosen.argv(spec)
    run_env = chosen.run_env(spec)
    facts = chosen.launch_facts(spec)  # before the spawn: Copilot's pre_spawn_at and $HOME session check
    scrub = clients.scrub_record(chosen)  # parent-session markers removed from the spawn env (names only)
    # O_EXCL prevents a second writer and ensures logs stay private even on errors.
    out_fd = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    err_fd = os.open(directory / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(out_fd, "wb") as output, os.fdopen(err_fd, "wb") as error:
            process, state_before = None, None
            try:
                state_before = (chosen.name, clients.static_env(chosen),
                                clients.persistent_state(chosen.name, clients.static_env(chosen)),
                                repo.common_dir / "coordinator" / "quarantine" / name) if plan else None
                spawned, record, copies = clients.boundary_launch(
                    plan, cwd, argv, clients.spawn_argv(chosen, argv, envelope)) if plan \
                    else (clients.spawn_argv(chosen, argv, envelope), None, {})
                try:
                    process = subprocess.Popen(spawned, cwd=cwd, pass_fds=tuple(copies.values()),
                                               stdin=subprocess.PIPE, stdout=output, stderr=error,
                                               start_new_session=True, umask=0o077,
                                               env={**(clients.boundary_spawn_env(plan, clients.static_env(chosen))
                                                       if plan else clients.static_env(chosen)), **run_env,
                                                    **profile.env, **(plan.env if plan else {})})
                finally:
                    boundary.close(copies)
                clients.own_group(process)
                seal(directory / "launch-intent.json", clients.launch_intent(process.pid, directory, session))
                launch = {"kind": "launch", "assignment_id": assignment_id,
                          "request_fingerprint": assignment["request_fingerprint"],
                          "worktree": str(repo.worktree), "common_dir": str(repo.common_dir),
                          "source_id": clients.source_id(chosen.name, "process"), "pid": str(process.pid),
                          "argv": argv, "pgid": process.pid, "model": model, "cli_version": version,
                          "client": chosen.name, "binary": binary,
                          "envelope_sha256": hashlib.sha256(envelope).hexdigest(),
                          "observed_at": datetime.now(timezone.utc).isoformat(),
                          **clients.launch_env(chosen, run_env), **facts}
                if chosen.confinement == "os-sandbox-requested":
                    launch["requested_sandbox"] = mode
                else:
                    launch.update(requested_mode="read" if read else mode, confinement=chosen.confinement)
                if read:   # V2, V5, V7: the staged inputs, their bound digests, the required reads, a qualification
                    launch.update(cwd=str(read_cwd), read_paths=staged, qualification=not qualified,
                                  read_digests={item["alias"]: item["digest"] for item in binding["read_inputs"]},
                                  required_reads=[item["alias"] for item in binding["read_inputs"] if item["required"]])
                if session is not None:
                    launch["session_id"] = session
                if spec.max_turns is not None:
                    launch["max_turns"] = spec.max_turns
                if profile_name:
                    launch.update(profile=profile_name, binding_sha256=bound["payload_digest"], env=profile.env)
                if record is not None:   # launch-boundary L5, inside the launch digest
                    launch["boundary"] = record
                launch_digest = seal(directory / "launch.json", launch)
                if scrub is not None:  # a sidecar, so Codex launch.json bytes stay unchanged
                    seal(directory / "env-scrub.json", scrub)
                source = ReceiptEvidenceSource(repo, assignment_id, assignment["request_fingerprint"], store)
                observed = Coordinator(store, source)
                observed.observe_launch(owner, assignment_id, LaunchEvidence(
                    launch["source_id"], receipt_ref(name, "launch"), launch["observed_at"],
                    launch["pid"], launch_digest))
                try:
                    process.communicate(input=envelope if chosen.envelope_via == "stdin" else b"",
                                        timeout=timeout_seconds)
                    timed_out, signals = False, []
                except subprocess.TimeoutExpired:
                    timed_out = True
                    signals = _stop_group(process)
                group = clients.drain_group(process, signals)  # N2: markers of an ended run follow the drain
                if timed_out:
                    private_file(directory / "timeout.json", canonical({"signals": signals, "pid": process.pid,
                                                                         "exit_code": process.poll(),
                                                                         "ended_at": datetime.now(timezone.utc).isoformat()}))
                seal(directory / "end.json", {"pid": process.pid, "pgid": process.pid,
                                               "exit_code": process.poll(),
                                               "ended_at": datetime.now(timezone.utc).isoformat(),
                                               "timed_out": timed_out})
                reason = _read_refusal(directory, launch, assignment, binding, profile) \
                    if read and not timed_out and process.returncode == 0 and group["group_terminated"] else None
                if reason:   # §10 R1: one repair round, the same session resumed with the exact refusal
                    repair_argv = clients.read_repair_argv(spec, session)
                    repair_envelope = canonical(clients.read_repair_envelope(reason, schema, {
                        "assignment_id": assignment["id"], "request_fingerprint": assignment["request_fingerprint"]}))
                    private_file(directory / clients.REPAIR_ENVELOPE, repair_envelope)
                    first_answer = _sha256_file(directory / "events.jsonl")
                    spawned, _, copies = clients.boundary_launch(
                        plan, cwd, repair_argv, clients.spawn_argv(chosen, repair_argv, repair_envelope))
                    out2 = os.open(directory / clients.REPAIR_STREAM,
                                   os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    err2 = os.open(directory / "stderr-repair.log",
                                   os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(out2, "wb") as output2, os.fdopen(err2, "wb") as error2:
                        try:
                            process = subprocess.Popen(spawned, cwd=cwd, pass_fds=tuple(copies.values()),
                                                       stdin=subprocess.PIPE, stdout=output2, stderr=error2,
                                                       start_new_session=True, umask=0o077,
                                                       env={**clients.boundary_spawn_env(plan, clients.static_env(chosen)),
                                                            **run_env, **profile.env, **plan.env})
                        finally:
                            boundary.close(copies)
                        clients.own_group(process)
                        try:
                            process.communicate(input=b"", timeout=timeout_seconds)
                            timed_out, signals = False, []
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            signals = _stop_group(process)
                    group = clients.drain_group(process, signals)
                    seal(directory / clients.REPAIR_RECORD, {
                        "kind": "read-repair", "reason": reason, "argv": repair_argv,
                        "envelope_sha256": hashlib.sha256(repair_envelope).hexdigest(),
                        "first_events_sha256": first_answer, "pid": process.pid, "exit_code": process.poll(),
                        "ended_at": datetime.now(timezone.utc).isoformat(), "timed_out": timed_out})
            except BaseException as exc:
                signals = _stop_group(process) if process is not None else []
                group = clients.drain_group(process, signals)
                try:
                    private_file(directory / "interrupted.json", canonical({
                        "pid": process.pid if process else None, "signals": signals,
                        "exit_code": process.poll() if process else None,
                        "ended_at": datetime.now(timezone.utc).isoformat()}))
                except OSError:
                    pass
                _integrity(repo, directory, baseline, group, git_before, state_before)
                if isinstance(exc, (OSError, DomainError, subprocess.SubprocessError)):
                    return _report(assignment_id, "unknown", directory, ["launch or process evidence interrupted"])
                raise
        record = _integrity(repo, directory, baseline, group, git_before, state_before)
        clean = clients.integrity_clean(record)
        failure = None   # design D29: a story attempt the coordinator saw end without a return
        if timed_out:
            if not (_story(profile.name) and not read and clean and group.get("group_terminated")):
                return _report(assignment_id, "unknown", directory, ["timeout; descendants and hooks may remain"])
            failure = _failure_stop("timeout", process, signals, timeout_seconds)
        if not clean:   # issue #5: the cause the record names, not every possible one
            return _report(assignment_id, "unknown", directory, [clients.integrity_cause(record)])
        try:
            after = inventory(repo.worktree)
            if materialized:   # BMAD snapshot design S7: ignored files are outside the inventory, compared here
                bmad_snapshot.guard(repo.worktree, repo.common_dir, store.read_snapshot()["events"],
                                    materialized["tree"], "UNKNOWN_EVIDENCE")
            if read and delta(cwd_before, inventory(read_cwd)):   # V5: nothing was written
                raise DomainError("UNKNOWN_EVIDENCE", "The read-mode run changed its staged inputs")
            # design D25: after a non-zero exit only a client stop, read from the stream, goes on
            early = None
            if process.returncode != 0 and failure is None:
                try:
                    early = clients.exited_terminal(chosen, directory, launch)
                except DomainError:   # design D29: a story attempt's crash or refused client is a failure stop
                    if not (_story(profile.name) and not read and group.get("group_terminated")):
                        raise
                    failure = _failure_stop("exit", process, signals, timeout_seconds)
            extras = {}
            if chosen.observe is not None:
                extras["observation_sha256"] = seal(directory / chosen.observation_name,
                                                    chosen.observe(directory, launch))
            if launcher is not None:  # fix round 2 (I2): the run's own launcher, sealed and rechecked before terminal
                state = clients.launcher_state(directory)
                extras["launcher_check_sha256"] = seal(directory / clients.LAUNCHER_CHECK, state)
                if not clients.launcher_intact(state, hashlib.sha256(launcher).hexdigest()) or \
                   launch.get("script_launcher", {}).get("sha256") != hashlib.sha256(launcher).hexdigest():
                    raise DomainError("UNKNOWN_EVIDENCE", "Script launcher changed during the run")
            if failure is not None:   # design D29: no terminal; the stop is sealed under the return's own checks
                if not os.path.lexists(directory / "last.json"):
                    private_file(directory / "last.json", b"")
                final = _stop_final(assignment, failure)
                event = _return_evidence(repo, assignment, directory, before, after, final, "process",
                                         process.returncode, profile, binding,
                                         json.loads(feature["writable_paths_json"]), chosen, extras)
                observed.anchor_return(owner, assignment_id, event)
                observed.record_return(owner, assignment_id, event)
                return _report(assignment_id, STATE_OF[final["result"]], directory)
            terminal = early or chosen.terminal(directory, launch)
            if terminal.facts.get("answer_error"):   # V6: the last answer (after the repair round, if any)
                raise DomainError("UNKNOWN_EVIDENCE", "Read-mode stream invalid: " + terminal.facts["answer_error"])
            if read and terminal.facts.get("repair"):
                extras["repair_sha256"] = _sha256_file(directory / clients.REPAIR_RECORD)
                extras["repair_events_sha256"] = _sha256_file(directory / clients.REPAIR_STREAM)
            if terminal.stop is not None and not _story(profile.name):   # design D25: a story-profile fact only
                raise DomainError("UNKNOWN_EVIDENCE", "the client stopped at its turn limit "
                                  f"({terminal.stop.get('num_turns')} turns) without a return")
            if chosen.return_source == "stdout" or read:   # V6: the read mode's return is the final answer
                private_file(directory / "last.json", terminal.final)
            elif chosen.return_source == "worker-file":
                private_file(directory / "last.json", clients.read_worker_return(directory))
                extras["worker_return_sha256"] = clients.worker_return_sha256(directory)
            last = (directory / "last.json").read_bytes()
            if last != terminal.final:
                raise DomainError("UNKNOWN_EVIDENCE", "Final message differs from the client terminal")
            facts = clients.client_facts(chosen, spec.mode, terminal, web=spec.web)
            if facts:
                extras["client_facts"] = facts
            try:
                final = _stop_final(assignment, terminal.stop) if terminal.stop is not None else \
                    _final(json.loads(last), assignment, profile, read)
                if read:
                    final = _observed_reads(final, binding, terminal.facts.get("reads", {}))
                event = _return_evidence(repo, assignment, directory, before, after, final, terminal.thread_id,
                                         process.returncode, profile, binding,
                                         json.loads(feature["writable_paths_json"]), chosen, extras)
            except (InvalidReturn, DomainError) as exc:
                # design D27: a story worker's refused return (its JSON, or the profile after the scope checks) is
                # a client stop of its own, committed and continued, never an operator's resolve
                story = _story(profile.name) and terminal.stop is None and not read
                if not story or (isinstance(exc, DomainError) and not (
                        exc.code == "UNKNOWN_EVIDENCE" and exc.message.startswith("Worker final JSON"))):
                    raise
                final = _stop_final(assignment, _invalid_return_stop(exc))
                event = _return_evidence(repo, assignment, directory, before, after, final, terminal.thread_id,
                                         process.returncode, profile, binding,
                                         json.loads(feature["writable_paths_json"]), chosen, extras)
            if not qualified:   # V1: a qualification run passes every check and is never anchored nor observed
                return _report(assignment_id, "unknown", directory, [
                    "qualification run: every check passed; never observed (Copilot reviewer design V1)"])
            observed.anchor_return(owner, assignment_id, event)
            observed.record_return(owner, assignment_id, event)
            return _report(assignment_id, STATE_OF[final["result"]], directory)
        except (OSError, ValueError, DomainError) as exc:
            return _report(assignment_id, "unknown", directory, [str(exc)])
    finally:
        pass


def inspect_run(repo: RepoIdentity, store: Store, assignment_id: str) -> RunReport:
    snapshot = store.read_snapshot()
    assignment = _row(snapshot, "assignments", assignment_id)
    directory = run_root(repo) / run_name(assignment_id, assignment["request_fingerprint"])
    if assignment["state"] in RETURNED:
        try:
            event = _validated_receipt(repo, store, assignment, directory)
            stored = json.loads(assignment["return_json"])
            if stored["provenance_class"] != "observed" or stored["payload_digest"] != event.payload_digest or \
               stored["event_ref"] != event.event_ref:
                raise DomainError("UNKNOWN_EVIDENCE", "Stored return is not this observed receipt")
            return _report(assignment_id, assignment["state"], directory,
                           [CONTINUATION_UNSUPPORTED] if assignment["state"] == "needs-decision" else ())
        except (OSError, KeyError, ValueError, TypeError, DomainError) as exc:
            return _report(assignment_id, "unknown", directory if directory.is_dir() else None, [str(exc)])
    return _report(assignment_id, assignment["state"], directory if directory.is_dir() else None,
                   ["verified terminal receipt and owner reconciliation needed"])


def _launch_intent(directory: Path) -> dict | None:
    """The adapter's launch-intent.json (the first post-spawn write), or None when absent or unreadable."""
    try:
        fd = os.open(directory / "launch-intent.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            value = json.loads(stream.read(1 << 16))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and type(value.get("pid")) is int and value["pid"] > 0 else None


def _proc_stat(pid: int) -> tuple[int, str] | None:
    """(start time in clock ticks, state letter) from /proc/<pid>/stat, or None when no such process."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rindex(")") + 2:].split()
        return int(fields[19]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _cmdline_hits(tokens: list[str]) -> list[dict]:
    """Every /proc/*/cmdline (other than this process) that contains a launch token; a point-in-time scan."""
    hits = []
    for name in sorted(os.listdir("/proc"), key=lambda item: (len(item), item)):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            data = Path(f"/proc/{name}/cmdline").read_bytes()
        except OSError:
            continue
        hits += [{"pid": int(name), "token": token} for token in tokens if token.encode() in data]
    return hits


def resolution_evidence(repo: RepoIdentity, store: Store, assignment_id: str) -> dict:
    """Read-only D15 evidence for the operator-only `attempt resolve`: the adapter launch intent (`spawn` is never,
    unrecorded or recorded), the recorded pid's liveness (alive: same pid, start time and boot; reused; exited; gone),
    a /proc cmdline scan for the launch tokens, and the current worktree inventory digest with its delta against the
    latest anchored observed inventory of that worktree. Point in time: a detached descendant with another cmdline
    escapes the scan."""
    snapshot = store.read_snapshot()
    assignment = _row(snapshot, "assignments", assignment_id)
    feature = _row(snapshot, "features", assignment["feature_id"])
    directory = run_root(repo) / run_name(assignment_id, assignment["request_fingerprint"])
    value = {"assignment_id": assignment_id, "state": assignment["state"], "run_dir": str(directory),
             "spawn": "never", "intent": None, "pid": None, "pid_state": None, "boot_id": clients._boot_id(),
             "launch_tokens": [], "cmdline_hits": []}
    if os.path.lexists(directory):
        intent = _launch_intent(directory)
        tokens = [str(directory)]
        if intent is None:
            value["spawn"] = "unrecorded"
        else:
            value.update(spawn="recorded", intent=intent, pid=intent["pid"])
            tokens = [item for item in (intent.get("run_dir"), str(directory), intent.get("session_id"))
                      if isinstance(item, str) and item]
            proc = _proc_stat(intent["pid"])
            boots = (intent.get("boot_id"), value["boot_id"])
            if proc is None:
                value["pid_state"] = "gone"
            elif (None not in boots and boots[0] != boots[1]) or \
                    (intent.get("start_time") is not None and proc[0] != intent.get("start_time")):
                value["pid_state"] = "reused"  # an unknown boot id decides on the start time alone
            else:
                value["pid_state"] = "exited" if proc[1] == "Z" else "alive"
        value["launch_tokens"] = list(dict.fromkeys(tokens))
        value["cmdline_hits"] = _cmdline_hits(value["launch_tokens"])
    worktree = os.path.realpath(feature["worktree"]) if feature["worktree"] else None
    try:
        current = stamp_json(inventory(Path(worktree))) if worktree else None
    except DomainError:
        current = None
    value["inventory_digest"] = hashlib.sha256(canonical(current)).hexdigest() if current is not None else None
    observed = {row["subject_id"]: row["revision"] for row in snapshot["events"]
                if row["kind"] == "assignment.return-observed"}
    worktrees = {row["id"]: os.path.realpath(row["worktree"]) for row in snapshot["features"] if row["worktree"]}
    candidates = sorted((observed[row["id"]], row["id"], row) for row in snapshot["assignments"]
                        if row["id"] in observed and row["return_json"] and worktree and
                        worktrees.get(row["feature_id"]) == worktree)
    value["baseline"], value["inventory_delta"] = None, None
    if candidates and current is not None:
        _, baseline_id, row = candidates[-1]
        try:
            ret = json.loads(row["return_json"])
            before = read_receipt(repo, ret["event_ref"], ret["payload_digest"])["after"]
            value["baseline"] = baseline_id
            value["inventory_delta"] = sorted(path for path in before.keys() | current.keys()
                                              if before.get(path) != current.get(path))
        except (DomainError, KeyError, TypeError, ValueError):
            value["baseline"] = None
    return value


def reconcile_run(repo: RepoIdentity, store: Store, owner: OwnerRef, assignment_id: str) -> RunReport:
    snapshot = store.read_export_snapshot(owner)
    assignment = _row(snapshot, "assignments", assignment_id)
    directory = run_root(repo) / run_name(assignment_id, assignment["request_fingerprint"])
    if assignment["state"] in RETURNED:
        return inspect_run(repo, store, assignment_id)
    if assignment["state"] not in ("launch-pending", "launch-unknown", "running"):
        raise DomainError("BLOCKED_EVIDENCE", "Assignment has no reserved attempt")
    try:
        event = _validated_receipt(repo, store, assignment, directory)
        source = ReceiptEvidenceSource(repo, assignment_id, assignment["request_fingerprint"], store)
        Coordinator(store, source).record_return(owner, assignment_id, event)
        return _report(assignment_id, STATE_OF[event.result], directory)
    except (OSError, KeyError, ValueError, DomainError) as exc:
        return _report(assignment_id, "unknown", directory if directory.is_dir() else None, [str(exc)])
