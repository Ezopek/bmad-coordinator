"""Epic drive (docs/epic-drive-design.md, E1–E15): every remaining story of an epic, in the epics file's order, through
the story drive on a coordinator-owned integration branch. One owner session `epic:<id>` for the whole run; the story
driver's `Run` is reused unchanged; the loop adds the base sync, the preconditions gate, `story add`, the pipeline
template, the integration merge and the epic's sealed, anchored state."""

import hashlib
import json
import os
import secrets
import signal
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import bmad_implementation as implementation
from . import bmad_planning, drive, drive_pipeline
from .drive import EPIC_SESSION, EXIT, TOKEN, Outcome, Refusal, RunBounds, _bounds_check, _hook, _now, _seal, _sealed
from .model import DecisionInput, DomainError, OwnerRef
from .run_receipt import canonical
from .service import Coordinator
from .store import open_store

FORMAT = 1
SAMPLE = {"__FEATURE__": "0" * 32, "__KEY__": "0-0-sample"}
RESTART = "epic-restart:"   # design epic-drive E15: the source_ref prefix of a restart's decision
EPIC_REASONS = ("precondition-unmet", "story-status", "integration-conflict", "worktree-drift", "epic-done")
STORY_STATES = ("pending", "current", "done", "skipped", "already-done", "blocked")


@dataclass(frozen=True)
class EpicInput:
    epics_path: str
    epic: int
    worktrees_dir: str
    story_defaults: dict
    branch: str | None = None
    base: str | None = None
    stories: dict = field(default_factory=dict)


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_INPUT", message)


def _relative(value, what: str) -> None:
    if type(value) is not str or not value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise _invalid(f"{what} must be a relative path")


def _scopes(value, what: str) -> dict:
    """`{writable_scopes?, project_clauses?}` of `story_defaults` or of one story's override."""
    if not isinstance(value, dict) or set(value) - {"writable_scopes", "project_clauses"}:
        raise _invalid(f"{what} takes writable_scopes and project_clauses only")
    for key, items in value.items():
        if not isinstance(items, list) or any(type(item) is not str or not item for item in items):
            raise _invalid(f"{what} {key} must be a list of non-empty strings")
    return value


def _roles_override(value, what: str) -> dict:
    """Design epic-drive E14: `{<role>: {timeout?, max_turns?}}` within the pipeline's own ranges."""
    if not isinstance(value, dict) or not value or set(value) - set(drive_pipeline.ROLES):
        raise _invalid(f"{what} roles must map one or more of {', '.join(drive_pipeline.ROLES)} to an override")
    for role, over in value.items():
        if not isinstance(over, dict) or not over or set(over) - {"timeout", "max_turns"}:
            raise _invalid(f"{what} roles.{role} takes timeout and max_turns only")
        if "timeout" in over:
            drive_pipeline._integer(over["timeout"], 1, drive_pipeline.MAX_TIMEOUT, f"{what} roles.{role} timeout")
        if "max_turns" in over:
            drive_pipeline._integer(over["max_turns"], 1, drive_pipeline.MAX_TURNS, f"{what} roles.{role} max_turns")
    return value


def check_input(repo, spec: EpicInput) -> dict:
    """Design epic-drive E13 (and ruling R1): the validated start input. `worktrees_dir` lies outside the repository
    worktree (a worktree inside the project would be untracked content a `git clean` destroys) and is a directory or
    absent; `branch` defaults to `coordinator/epic-<N>` and must be a plain branch name, the very name Git checks (no
    `@{-N}` expansion); `base` is any ref name, resolved at start."""
    _relative(spec.epics_path, "epics_path")
    if type(spec.epic) is not int or spec.epic < 1:
        raise _invalid("epic must be a positive integer")
    defaults = _scopes(spec.story_defaults, "story_defaults")
    if not defaults.get("writable_scopes"):
        raise _invalid("story_defaults needs writable_scopes")
    if not isinstance(spec.stories, dict) or \
       any(type(key) is not str or not implementation.STORY_KEY.match(key) for key in spec.stories):
        raise _invalid("stories must map story keys to overrides")
    for key, value in spec.stories.items():
        if not isinstance(value, dict):
            raise _invalid(f"stories.{key} must be an object")
        _scopes({name: item for name, item in value.items() if name != "roles"}, f"stories.{key}")
        if "roles" in value:   # design epic-drive E14: a story's own worker box
            _roles_override(value["roles"], f"stories.{key}")
    if type(spec.worktrees_dir) is not str or not spec.worktrees_dir:
        raise _invalid("worktrees_dir is required")
    worktrees = Path(spec.worktrees_dir).absolute()
    inside, top = os.path.realpath(worktrees), os.path.realpath(repo.worktree)
    if inside == top or inside.startswith(top + os.sep):
        raise DomainError("UNSAFE_PATH", "worktrees_dir must lie outside the repository worktree")
    if any(parent.is_symlink() for parent in (worktrees, *worktrees.parents)):
        raise DomainError("UNSAFE_PATH", "worktrees_dir must not use symlinks")
    if os.path.lexists(worktrees) and not worktrees.is_dir():
        raise _invalid("worktrees_dir exists and is not a directory")
    branch = spec.branch if spec.branch is not None else f"coordinator/epic-{spec.epic}"
    if type(branch) is not str or not branch or branch.startswith("refs/"):
        raise _invalid("branch is not a valid branch name")
    checked = implementation._git_run(repo.worktree, ["check-ref-format", "--branch", branch], check=False)
    if checked.returncode != 0 or checked.stdout.decode("utf-8", "replace").strip() != branch:
        raise _invalid("branch is not a valid branch name")
    if spec.base is not None and (type(spec.base) is not str or not spec.base):
        raise _invalid("base must be a ref name")
    return {"epics_path": spec.epics_path, "epic": spec.epic, "worktrees_dir": str(worktrees), "branch": branch,
            "base": spec.base, "story_defaults": defaults, "stories": dict(spec.stories)}


# Template (E8) ------------------------------------------------------------------------------------------------------

def fill_template(template: dict, feature_id: str, story_key: str, roles: dict | None = None,
                  revision: int = 1) -> dict:
    """Design epic-drive E8, E14 and E15: every string value with `__FEATURE__` and `__KEY__` replaced, a story's
    roles override merged over the template's roles, the mandate revision set (a restart binds above 1), then the
    validated pipeline."""
    def fill(value):
        if isinstance(value, str):
            return value.replace("__FEATURE__", feature_id).replace("__KEY__", story_key)
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value
    filled = fill(template)
    if roles:
        missing = set(roles) - set(filled.get("roles") or {})
        if missing:
            raise _invalid(f"roles override names a role the template lacks: {', '.join(sorted(missing))}")
        filled["roles"] = {name: {**role, **roles.get(name, {})} for name, role in filled["roles"].items()}
    filled["mandate_revision"] = revision
    return drive_pipeline.validate(filled)


def _check_overrides(template: dict, stories: dict, bounds: RunBounds, finished=frozenset()) -> None:
    """Design epic-drive E14 (rulings H6 and I2): the roles override of every story still to run (its key not in
    `finished`) fills, validates and fits the bounds; every refusal names the story. `epic start` checks them all;
    `epic resume` passes the stories its newest state has finished (`done`, `skipped`, `already-done`) and the one
    its `--skip` or `--restart` names; a restart checks the story's effective override itself (E15)."""
    for key, over in stories.items():
        if over.get("roles") and key not in finished:
            try:
                _bounds_check(fill_template(template, SAMPLE["__FEATURE__"], key, over["roles"]), bounds)
            except Refusal as exc:
                raise Refusal(exc.code, f"Story {key}: {exc.message}", exc.extra["reason"],
                              exc.extra.get("action")) from None
            except DomainError as exc:
                raise DomainError(exc.code, f"Story {key}: {exc.message}") from None


def check_template(template) -> dict:
    """Design epic-drive E8: a story pipeline whose `feature_id` is `__FEATURE__`, whose step `story` blocks name
    `__KEY__` (in `story_key` and inside `spec_path`), with a `spec` step at mandate revision 1; validated once with
    sample values. Returns the template as canonical JSON."""
    if not isinstance(template, dict):
        raise _invalid("Pipeline template must be one JSON object")
    if template.get("feature_id") != "__FEATURE__":
        raise _invalid("Pipeline template feature_id must be __FEATURE__")
    if template.get("mandate_revision") != 1:
        raise _invalid("Pipeline template binds at mandate revision 1")
    steps = template.get("steps") if isinstance(template.get("steps"), list) else []
    if not any(isinstance(step, dict) and step.get("kind") == "spec" for step in steps):
        raise _invalid("Pipeline template needs a spec step: every epic story is written from its epic entry")
    for step in steps:
        story = step.get("story") if isinstance(step, dict) else None
        if isinstance(story, dict) and \
           (story.get("story_key") != "__KEY__" or "__KEY__" not in str(story.get("spec_path"))):
            raise _invalid("Pipeline template story blocks name __KEY__ in story_key and spec_path")
    fill_template(template, SAMPLE["__FEATURE__"], SAMPLE["__KEY__"])
    return json.loads(canonical(template))


# Stories (E5) -------------------------------------------------------------------------------------------------------

def sprint_entries(text: str) -> dict[str, str]:
    """The `development_status` map as `{key: status}`; the map ends at the next top-level key (D18's rule)."""
    lines = implementation._plain(text)[2].split("\n")
    start = next((index for index, line in enumerate(lines) if line.rstrip() == "development_status:"), None)
    if start is None:
        raise DomainError("STALE_INPUT", "story-status: sprint-status.yaml has no development_status map")
    entries = {}
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            break
        match = implementation._STATUS_LINE.match(line)
        if match is not None:
            entries[match.group(2).strip()] = implementation._unquote(match.group(3))
    return entries


def plan_stories(repo, commit: str, epics_path: str, epic: int) -> list[dict]:
    """Design epic-drive E5: the `### Story <epic>.<M>:` headings of the epics file at `commit`, in order, each with
    the one `development_status` key starting `<epic>-<M>-` and its status; `key` is None (and `keys` the candidates)
    when none or several match. No story of the epic is `STALE_INPUT`."""
    epics = implementation._read_at(repo, commit, epics_path, "Epics file")
    sprint = implementation._read_at(repo, commit, f"{implementation.ARTIFACTS_DIR}/sprint-status.yaml",
                                     "Sprint status")
    entries = sprint_entries(sprint)
    text = epics.lstrip("﻿").replace("\r\n", "\n")
    found = []
    for match in implementation._STORY_HEADING.finditer(text):
        if int(match.group(1)) != epic:
            continue
        prefix = f"{match.group(1)}-{match.group(2)}-"
        keys = [key for key in entries if key.startswith(prefix)]
        key = keys[0] if len(keys) == 1 else None
        found.append({"number": f"{match.group(1)}.{match.group(2)}", "title": match.group(3).strip(), "key": key,
                      "keys": keys, "status": entries.get(key) if key else None})
    if not found:
        raise DomainError("STALE_INPUT", f"story-status: the epics file has no stories of epic {epic}")
    return found


# Git (E2–E4) --------------------------------------------------------------------------------------------------------

def _rev(worktree: Path, ref: str) -> str | None:
    done = implementation._git_run(worktree, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                              f"{ref}^{{commit}}"], check=False)
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else None


def create_integration(repo, branch: str, base: str | None, worktree: Path) -> dict:
    """Design epic-drive E2 (ruling R1): the integration branch at the base commit (`base`, default `HEAD` of the
    `--repo` worktree) with its linked worktree. Refuses an existing branch, an existing worktree path and an
    unresolvable base before any write; when `worktree add` fails after Git created the branch, the branch (still at
    the base commit) and the worktree record are removed, so a retry is not refused as `branch-exists`. `base_branch` is
    the branch `base` names, a real `refs/heads/` ref and no revision expression (for `HEAD`, the checked-out branch),
    else None: no tracking."""
    if _rev(repo.worktree, f"refs/heads/{branch}") is not None:
        raise Refusal("INVALID_INPUT", f"Branch {branch} exists", "branch-exists",
                      "epic resume continues an epic; a new epic takes another branch name")
    if os.path.lexists(worktree):
        raise Refusal("INVALID_INPUT", "Epic worktree path exists", "worktree-exists")
    ref = base if base is not None else "HEAD"
    base_commit = _rev(repo.worktree, ref)
    if base_commit is None:
        raise _invalid(f"base {ref} does not resolve to a commit")
    if ref == "HEAD":
        found = implementation._git_run(repo.worktree, ["symbolic-ref", "--quiet", "HEAD"], check=False)
        name = found.stdout.decode("utf-8", "replace").strip().removeprefix("refs/heads/") if found.returncode == 0 \
            else None
    else:
        short = base.removeprefix("refs/heads/")
        known = implementation._git_run(repo.worktree, ["show-ref", "--verify", "--quiet", f"refs/heads/{short}"],
                                        check=False).returncode == 0
        name = short if known and _rev(repo.worktree, f"refs/heads/{short}") == base_commit else None
    try:
        implementation._git_run(repo.worktree, ["worktree", "add", "-b", branch, str(worktree), base_commit])
    except DomainError:
        implementation._git_run(repo.worktree, ["update-ref", "-d", f"refs/heads/{branch}", base_commit], check=False)
        implementation._git_run(repo.worktree, ["worktree", "prune"], check=False)
        raise
    return {"base_commit": base_commit, "base_branch": name}


def check_worktree(worktree: Path, branch: str) -> None:
    """Design epic-drive E4: the epic worktree has the integration branch checked out and no tracked change; else
    `worktree-drift` (untracked files are the operator's and do not count)."""
    implementation._on_branch(worktree, branch)
    dirty = implementation._git_run(worktree, ["status", "--porcelain", "--untracked-files=no"]).stdout
    if dirty.strip():
        raise DomainError("STALE_INPUT", "worktree-drift: the epic worktree has uncommitted changes")


def merge_into(worktree: Path, ref: str, message: str, project: dict) -> dict:
    """Design epic-drive E3/E4: `merge --ff-only ref`; when that is refused, `merge --no-ff --no-verify -m message` by
    the project identity (hooks off through the isolated Git, `--no-log` so no `merge.log` rewrites the message); a
    merge Git refuses or that conflicts is aborted and reported `conflict` with the conflicting paths (read before the
    abort, E10) and Git's words, which always means a clean worktree: an abort that leaves a tracked change or
    `MERGE_HEAD` is INVALID_REPOSITORY. `already` when `ref` is an ancestor of HEAD."""
    before = implementation._git_text(worktree, "rev-parse", "HEAD")
    if _rev(worktree, ref) is None:
        return {"outcome": "conflict", "detail": f"{ref} does not resolve to a commit", "head": before}
    done = implementation._git_run(worktree, ["merge", "--ff-only", "--quiet", ref], check=False)
    if done.returncode == 0:
        head = implementation._git_text(worktree, "rev-parse", "HEAD")
        return {"outcome": "fast-forward" if head != before else "already", "head": head}
    done = implementation._git_run(worktree, ["merge", "--no-ff", "--no-verify", "--no-log", "--quiet", "-m", message,
                                              ref], env_extra=implementation._identity_env(project), check=False)
    if done.returncode == 0:
        return {"outcome": "merge", "head": implementation._git_text(worktree, "rev-parse", "HEAD")}
    paths = implementation._git_text(worktree, "diff", "--name-only", "--diff-filter=U").splitlines()
    implementation._git_run(worktree, ["merge", "--abort"], check=False)   # fails when no merge started: no MERGE_HEAD
    left = implementation._git_text(worktree, "status", "--porcelain", "--untracked-files=no")
    if left or _rev(worktree, "MERGE_HEAD") is not None:
        raise DomainError("INVALID_REPOSITORY",
                          f"merge abort left {worktree} dirty: {(left or 'MERGE_HEAD remains')[:200]}")
    words = done.stderr.decode("utf-8", "replace").strip() or done.stdout.decode("utf-8", "replace").strip()
    named = f"conflicting paths: {', '.join(paths)}; " if paths else ""
    return {"outcome": "conflict", "detail": (named + words)[:300], "head": before}


# State (E9) ---------------------------------------------------------------------------------------------------------

def _epic_dir(base: Path, epic_id: str) -> Path:
    return drive._directory(base / "epics" / epic_id)


def _seal_record(store, path: Path, value: dict) -> tuple[str, str]:
    """Design epic-drive E9/E10: seal an epic record whose name counts (`state-<revision>.json`,
    `summary-<n>.json`). An existing file an anchored event references is that record: identical bytes are anchored
    again as they are, other bytes are UNKNOWN_EVIDENCE. An existing file no event references is the orphan of a kill
    between the seal and its anchor (its bytes differ, they carry a timestamp): it is removed and `value` sealed in its
    place. The epic holds `drive.lock` exclusively, so no other run anchors in between."""
    base = store.path.parent
    try:
        return _seal(base, path, value)
    except FileExistsError:
        ref = path.relative_to(base).as_posix()
        if any(row["evidence_ref"] == ref for row in store.read_snapshot()["events"]):
            digest = hashlib.sha256(canonical(value)).hexdigest()
            drive_pipeline._read_sealed(base, ref, digest)
            return ref, digest
        os.unlink(path)
        return _seal(base, path, value)


def anchor_state(store, owner: OwnerRef, state: dict, kind: str, source: str) -> tuple[str, str]:
    """Design epic-drive E9: seal epics/<id>/state-<revision>.json and anchor it (`epic.started` for revision 1,
    `epic.advanced` after); a state file a kill left unanchored is replaced (`_seal_record`)."""
    base = store.path.parent
    ref, digest = _seal_record(store, _epic_dir(base, state["epic_id"]) / f"state-{state['revision']}.json", state)
    Coordinator(store).anchor_event(owner, kind, state["epic_id"], (source, ref, digest))
    _hook("after-epic-advance", revision=state["revision"], source=source)
    return ref, digest


def newest_state(store, epic_id: str, snapshot=None) -> dict | None:
    """Design epic-drive E9: the newest anchored state of the epic, or None."""
    events = (snapshot or store.read_snapshot())["events"]
    rows = [row for row in events if row["kind"] in ("epic.started", "epic.advanced") and row["subject_id"] == epic_id]
    return _sealed(store.path.parent, rows[-1]) if rows else None


def epic_ids(snapshot: dict) -> list[str]:
    seen = []
    for row in snapshot["events"]:
        if row["kind"] == "epic.started" and row["subject_id"] not in seen:
            seen.append(row["subject_id"])
    return seen


def find_epic(store, owner: OwnerRef) -> dict:
    """Design epic-drive E11: the epic of the owner session `epic:<id>`, its newest state at the owner's generation."""
    session = store.read_snapshot()["owner"]["session_id"] or ""
    if not session.startswith(EPIC_SESSION):
        raise Refusal("RECOVERY_BLOCKED", "The owner session is not an epic run", "run-unbound",
                      "epic start; or, while no session owns the repository, epic resume --epic <id> "
                      "with a fresh token file")
    epic_id = session[len(EPIC_SESSION):]
    state = newest_state(store, epic_id)
    if state is None:   # killed before the first state (§7): nothing to resume, the branch is removed by hand
        raise Refusal("RECOVERY_BLOCKED", "The owner session has no anchored epic state", "run-unbound",
                      f"release --token-file {TOKEN}, then epic start")
    if state.get("generation") != owner.generation:   # an E12 re-claim killed before its state: claim again
        raise Refusal("RECOVERY_BLOCKED", "The owner session has no anchored epic state", "run-unbound",
                      f"release --token-file {TOKEN}, then epic resume --epic {epic_id} --token-file <new file>")
    return state


# The run (E1, E3–E7, E9–E12) ---------------------------------------------------------------------------------------

def _epic_action(reason: str, subjects: dict) -> str | None:
    """The operator action of an epic-level stop (design epic-drive E10)."""
    key = subjects.get("key") or subjects.get("number")
    if reason == "precondition-unmet":
        return (f"record the decision on entry {subjects.get('entry')} (a settling `decision:` line or `status: done` "
                "in deferred-work.md) on the base branch or the integration branch, then epic resume; or epic resume "
                f"--skip {key} to leave story {key} to the operator")
    if reason == "story-status":
        return (f"{subjects.get('detail')}: finish or re-status the story by hand (sprint-status.yaml on the base or "
                f"the integration branch), then epic resume; or epic resume --skip {key}")
    if reason == "integration-conflict":
        return (f"merge by hand: git -C {subjects.get('worktree')} merge {subjects.get('ref')}, resolve the conflict, "
                "commit, then epic resume (it records the merge it finds); or release --token-file "
                f"{TOKEN} and finish attended")
    if reason == "worktree-drift":
        return (f"restore the epic worktree {subjects.get('worktree')}: check out {subjects.get('branch')} and commit "
                f"or discard its changes ({subjects.get('detail')}), then epic resume")
    return None


def _loop_action(reason: str, subjects: dict, epic_id: str) -> str | None:
    """Design epic-drive E10: the operator action of a failure the epic loop raises itself, which the story drive's
    text (`… then drive start`) does not fit: `story-add`, and the `stale-input`, `corrupt-state` and `internal-error`
    of a refused Git, store or evidence read. None otherwise (a signal stop, the deadline: the story drive's text read
    with `epic resume` holds)."""
    if reason == "story-add":
        key = subjects.get("key")
        return (f"remove story/{key} and its worktree (or epic resume --skip {key}), then epic resume --token-file "
                f"{TOKEN}")
    if reason in ("stale-input", "corrupt-state", "internal-error"):
        return (f"repair, then epic resume --token-file {TOKEN}; or release --token-file {TOKEN}, then epic resume "
                f"--epic {epic_id} --token-file <new file>")
    return None


# Ruling H10: the terminal story stops `--restart` follows; any other stop is answered (an inbox decision, an accept).
RESTARTABLE = ("critical-finding", "open-high-findings", "correction-budget", "update-budget", "worker-blocked",
               "attempt-resolved")


def _story_epic_action(outcome: Outcome, story: dict | None, epic_id: str, from_story: bool = False) -> str | None:
    """Ruling R3: the epic-level options beside a story's own operator action; none for an epic-level stop, which is
    decided by origin: a story run's outcome is never epic-level, whatever its reason (`worktree-drift`). `--restart`
    is named after a terminal stop only (ruling H10)."""
    if outcome.outcome == "done" or story is None or (outcome.reason in EPIC_REASONS and not from_story):
        return None
    key = story.get("key") or story["number"]
    restart = (f"epic resume --restart {key} [--roles <json file>] (same conditions) revises the story's mandate, "
               "binds the template afresh at the new revision with corrections at zero and runs the next attempt; "
               if outcome.reason in RESTARTABLE else "")
    return (f"epic resume continues story {key}; epic resume --skip {key} (allowed once this run has ended needs-human "
            "or failed and no attempt of the story holds the fence) leaves it to the operator and continues with the "
            f"next story; {restart}release --token-file {TOKEN} hands the repository back, after which epic resume "
            f"--epic {epic_id} --token-file <new file> continues")


def _restart_carry(summary: dict, snapshot: dict, accept_on_high: bool) -> dict | None:
    """Design epic-drive E15 (ruling H8): the terminal stop's recorded evidence a restart carries into the new
    revision's briefs: the items of the review a `critical-finding` or `open-high-findings` stop names (its
    `finding_ids`, else its blocking pointers under the template's `accept_on_high`), or the failed command names of
    a `correction-budget` stop's red receipt; None for any other stop."""
    reason, subjects = summary.get("reason"), summary.get("subjects") or {}
    if reason in ("critical-finding", "open-high-findings"):
        row = next((item for item in snapshot["reviews"] if item["id"] == subjects.get("review_id")), None)
        if row is None:
            raise DomainError("BLOCKED_EVIDENCE",
                              f"The stop names review {subjects.get('review_id')}, which the store does not hold")
        pointers = subjects.get("finding_ids") or drive_pipeline.blocking_findings(row["finding_json"], accept_on_high)
        items = bmad_planning._review_items(json.loads(row["finding_json"])["finding"], pointers)
        return {"kind": "review", "review_id": row["id"], "items": items}
    if reason == "correction-budget":
        return {"kind": "verify", "verify_ref": subjects.get("verify_ref"),
                "failed": list(subjects.get("failed") or [])}
    return None


class Epic:
    """Design epic-drive E1: one run (a start or a resume) of an epic under the owner session `epic:<id>`."""

    def __init__(self, repo, store, owner: OwnerRef, state: dict, template: dict, bounds: RunBounds, clock):
        self.repo, self.store, self.owner, self.state = repo, store, owner, state
        self.template, self.bounds, self.clock = template, bounds, clock
        self.base = store.path.parent
        self.started = clock()
        self.stopping = False
        self.counts = {"runs": 0, "merges": 0, "syncs": 0, "stories_done": 0, "stories_skipped": 0,
                       "stories_restarted": 0}   # this run's
        self.story_outcome = False   # the outcome ending the run came from a story run (E10, ruling R3)
        self.sample = fill_template(template, SAMPLE["__FEATURE__"], SAMPLE["__KEY__"])   # the deadline floor (E9)
        ended = [row for row in store.read_snapshot()["events"] if row["kind"] == "epic.ended" and
                 row["subject_id"] == state["epic_id"]]
        self.run_id = f"run-{len(ended) + 1:03d}-{secrets.token_hex(3)}"

    # Signals: between story runs a signal ends the epic at the next boundary; a story run installs its own handlers.
    def _on_signal(self, signum, frame):
        self.stopping = True

    @contextmanager
    def _signals(self):
        previous = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, self._on_signal)
        except ValueError:   # not the main thread
            pass
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def execute(self) -> tuple[dict, int]:
        with self._signals():
            try:
                outcome = self._loop()
            except DomainError as exc:
                outcome = self._failure(exc)
            except Exception as exc:  # noqa: BLE001 - every other failure ends the run honestly
                outcome = Outcome("failed", "internal-error", {"detail": f"{type(exc).__name__}: {exc}"[:300]})
        return self._end(outcome)

    @staticmethod
    def _failure(exc: DomainError) -> Outcome:
        if exc.code == "OWNERSHIP_CONFLICT":
            return Outcome("failed", "ownership-lost")
        if exc.code in ("CORRUPT_STATE", "INCOMPATIBLE_SCHEMA"):
            return Outcome("failed", "corrupt-state", {"detail": exc.message})
        if exc.code in ("STALE_INPUT", "BLOCKED_EVIDENCE", "UNKNOWN_EVIDENCE", "INVALID_REPOSITORY", "UNSAFE_PATH"):
            return Outcome("needs-human", "stale-input", {"gate": exc.code, "detail": exc.message})
        return Outcome("failed", "internal-error", {"gate": exc.code, "detail": exc.message})

    # State -----------------------------------------------------------------------------------------------------
    def _current(self) -> dict | None:
        return next((item for item in self.state["stories"] if item["state"] == "current"), None)

    def _advance(self, source: str, **changes) -> None:
        self.state = {**self.state, **changes, "revision": self.state["revision"] + 1, "advanced_at": _now()}
        anchor_state(self.store, self.owner, self.state, "epic.advanced", source)

    def _updated(self, number: str, **changes) -> list[dict]:
        return [{**item, **changes} if item["number"] == number else item for item in self.state["stories"]]

    def _feature(self, story: dict) -> dict:
        return next(row for row in self.store.read_export_snapshot(self.owner)["features"]
                    if row["id"] == story["feature_id"])

    def _lease_record(self, lease_id: str) -> dict:
        """Design epic-drive E7/E12: the newest anchored record of the lease (a D24 carry and an E12 reseal anchor a
        further one); UNKNOWN_EVIDENCE when it does not read back."""
        rows = [row for row in self.store.read_snapshot()["events"] if row["kind"] == "drive.leased" and
                row["subject_id"] == lease_id]
        lease = _sealed(self.base, rows[-1]) if rows else None   # a carry re-seals the lease: the newest decides
        if lease is None:
            raise DomainError("UNKNOWN_EVIDENCE", f"lease {lease_id} has no readable anchored record")
        return lease

    def _owned(self, lease: dict) -> dict:
        """Design epic-drive E12: a lease of an older generation (leased before a kill, then re-claimed; or an E12
        re-claim killed before its re-seal) is re-sealed under the owner's generation before it is reused."""
        if lease["generation"] == self.owner.generation:
            return lease
        return drive.reseal_lease(self.store, self.owner, lease, f"lease-g{self.owner.generation}")

    def _last_summary(self, lease_id: str) -> dict | None:
        """Design epic-drive E11: the sealed summary of the lease's last run; None without a run or when the last run
        has no end (killed)."""
        events = self.store.read_snapshot()["events"]
        runs = [row["source_id"] for row in events if row["kind"] == "drive.started" and row["subject_id"] == lease_id]
        if not runs:
            return None
        end = next((row for row in events if row["kind"] in ("drive.ended", "drive.stop-acknowledged") and
                    row["subject_id"] == lease_id and row["source_id"] == runs[-1]), None)
        return _sealed(self.base, end) if end is not None else None

    def _check(self) -> Outcome | None:
        """E4: before a write to the epic worktree."""
        try:
            check_worktree(Path(self.state["worktree"]), self.state["branch"])
        except DomainError as exc:
            if exc.code == "STALE_INPUT" and exc.message.startswith("worktree-drift"):
                return Outcome("needs-human", "worktree-drift", {"worktree": self.state["worktree"],
                                                                 "branch": self.state["branch"], "detail": exc.message})
            raise
        return None

    # The loop --------------------------------------------------------------------------------------------------
    def _loop(self) -> Outcome:
        """Design epic-drive E1/E9/E11: the boundary for a story without a feature, then the story's lease, its run
        (unless the lease's last run ended done) and its integration, until a stop or `epic-done`; every step starts
        from the newest anchored state, so a resume continues where a run ended or was killed."""
        while True:
            if self.stopping:
                return Outcome("needs-human", "signal-stop", {})
            story = self._current()
            if story is None or story.get("feature_id") is None:
                story = self._boundary(story)
                if isinstance(story, Outcome):
                    return story
            if story.get("lease_id") is None:
                story = self._lease(story)
            last = self._last_summary(story["lease_id"])
            if last is None or last.get("outcome") != "done":
                if self.stopping:   # a signal during the boundary work starts no story run
                    return Outcome("needs-human", "signal-stop", {})
                outcome = self._run(story)
                if outcome is not None:
                    return outcome
            outcome = self._integrate(story)
            if outcome is not None:
                return outcome

    def _boundary(self, story: dict | None):
        """Design epic-drive E3/E5/E6/E7/E9, before every story add: the base sync, the re-listing, the deadline floor,
        the preconditions gate and `story add`; the current story with its feature, or the outcome that ends the run.
        Under the floor nothing is synced, added or leased (the listing still runs, so a finished epic ends done). A
        current story whose `story/<key>` branch exists (killed after `story add`, before its record) goes straight
        to the adoption, without a sync, a re-listing or a gate, so a moved base cannot make its own branch look
        foreign; the sync happens at the next boundary."""
        short = self._below_floor()
        adopting = story is not None and _rev(self.repo.worktree, f"refs/heads/story/{story['key']}") is not None
        if not adopting:
            if not short:
                outcome = self._sync_base()
                if outcome is not None:
                    return outcome
            picked = self._pick()
            if isinstance(picked, Outcome):
                return picked
            if picked is None:
                return Outcome("done", "epic-done", {"skipped": [item["key"] for item in self.state["stories"]
                                                                 if item["state"] == "skipped"]})
            story = picked
        if short or self._below_floor(story):   # E14: the picked story's own box decides too
            return Outcome("continue", "deadline", {"key": story["key"]})
        if not adopting:
            outcome = self._gate(story)
            if outcome is not None:
                return outcome
        if self.stopping:   # a signal during the boundary work adds no story
            return Outcome("needs-human", "signal-stop", {})
        return self._add(story)

    def _sample(self, story: dict | None = None) -> dict:
        """Design epic-drive E14: the template sample, or the picked story's merged pipeline when it has a roles
        override."""
        over = self.state["stories_input"].get(story["key"]) if story and story.get("key") else None
        if not over or not over.get("roles"):
            return self.sample
        return fill_template(self.template, SAMPLE["__FEATURE__"], story["key"], over["roles"])

    def _below_floor(self, story: dict | None = None) -> bool:
        """Design epic-drive E9 and E14: what is left of this run's deadline cannot hold a story run (the largest role
        timeout of the template, or of the story's merged pipeline, plus the driver's MARGIN, as `_bounds_check`
        counts it), so no story is added or started."""
        try:
            _bounds_check(self._sample(story), self._remaining())
        except Refusal:
            return True
        return False

    def _sync_base(self) -> Outcome | None:
        """E3: the base branch merged into the integration branch when it moved since the last sync."""
        name = self.state["base_branch"]
        if name is None:
            return None
        head = _rev(self.repo.worktree, f"refs/heads/{name}")
        if head is None or head == self.state["base_synced_sha"]:
            return None
        drift = self._check()
        if drift is not None:
            return drift
        result = merge_into(Path(self.state["worktree"]), f"refs/heads/{name}",
                            f"epic({self.state['epic']}): sync {name} at {head[:12]}", self.template["project"])
        if result["outcome"] == "conflict":
            return Outcome("needs-human", "integration-conflict", {
                "merge": "base", "ref": f"refs/heads/{name}", "sha": head, "detail": result["detail"],
                "worktree": self.state["worktree"]})
        self.counts["syncs"] += 1
        self._advance("sync", base_synced_sha=head, head_sha=result["head"])
        return None

    def _pick(self):
        """E5 (ruling R4): the stories re-listed at the integration head; done, skipped and current records stand; the
        first pending story becomes current unless one is current already (a resume before its story add). A story the
        listing cannot place is `blocked` and stops the epic. The head is the integration branch's ref, not the epic
        worktree's HEAD: a detached or switched worktree cannot move the listing or the gate (E4's check stops it
        before the merge)."""
        head = _rev(self.repo.worktree, f"refs/heads/{self.state['branch']}")
        if head is None:
            raise DomainError("INVALID_REPOSITORY", f"The integration branch {self.state['branch']} does not resolve")
        try:
            plan = plan_stories(self.repo, head, self.state["epics_path"], self.state["epic"])
        except DomainError as exc:
            if exc.code == "STALE_INPUT":
                return Outcome("needs-human", "story-status", {"detail": exc.message})
            raise
        known = {item["number"]: item for item in self.state["stories"]}
        stories = []
        for entry in plan:
            old = known.get(entry["number"])
            if old is not None and old["state"] in ("done", "skipped", "current"):
                stories.append(old)
                continue
            item = {key: value for key, value in (old or {}).items() if key != "detail"}
            item.update(number=entry["number"], title=entry["title"], key=entry["key"])
            if entry["key"] is None:
                item.update(state="blocked", detail=f"story {entry['number']} has {len(entry['keys'])} sprint-status "
                                                    f"keys: {', '.join(entry['keys']) or 'none'}")
            elif entry["status"] == "done":
                item["state"] = "already-done"
            elif entry["status"] == "backlog":
                item["state"] = "pending"
            else:
                item.update(state="blocked", detail=f"story {entry['key']} is {entry['status']!r} in sprint-status, "
                                                    "not backlog or done")
            stories.append(item)
        blocked = next((item for item in stories if item["state"] == "blocked"), None)
        current = any(item["state"] == "current" for item in stories)
        chosen = None if blocked or current else next((item for item in stories if item["state"] == "pending"), None)
        if chosen is not None:
            stories = [{**item, "state": "current"} if item is chosen else item for item in stories]
        if stories != self.state["stories"] or head != self.state["head_sha"]:
            self._advance(chosen["key"] if chosen else "list", stories=stories, head_sha=head)
        if blocked is not None:
            status = next((entry["status"] for entry in plan if entry["number"] == blocked["number"]), None)
            return Outcome("needs-human", "story-status", {"number": blocked["number"], "key": blocked.get("key"),
                                                           "status": status, "detail": blocked["detail"]})
        return self._current()

    def _gate(self, story: dict) -> Outcome | None:
        """E6: every precondition bullet met at the integration head, else `precondition-unmet`."""
        head = self.state["head_sha"]
        epics = implementation._read_at(self.repo, head, self.state["epics_path"], "Epics file")
        entry = implementation.parse_epic_story(epics, story["number"])
        found = implementation.preconditions_state(self.repo, head, implementation.ARTIFACTS_DIR, entry.preconditions)
        unmet = [item for item in found if item["state"] != "met"]
        if not unmet:
            return None
        return Outcome("needs-human", "precondition-unmet", {
            "key": story["key"], "number": story["number"], "entry": unmet[0]["id"], "bullet": unmet[0]["bullet"],
            "detail": unmet[0]["reason"],
            "unmet": [{"id": item["id"], "bullet": item["bullet"], "reason": item["reason"]} for item in unmet]})

    def _add(self, story: dict):
        """E7: `story add` with the integration branch as base; a story branch a killed run left is adopted when it
        bases on the integration head."""
        snapshot = self.store.read_export_snapshot(self.owner)
        branch = f"story/{story['key']}"
        existing = next((row for row in snapshot["features"] if row["branch"] == branch), None)
        if existing is not None:
            if existing["base_commit"] != self.state["head_sha"]:
                return Outcome("failed", "story-add", {"key": story["key"],
                                                       "detail": f"branch {branch} exists with another base "
                                                                 f"({existing['base_commit'][:12]})"})
            feature_id, base_sha = existing["id"], existing["base_commit"]
        else:
            over = self.state["stories_input"].get(story["key"], {})
            defaults = self.state["story_defaults"]
            worktree = str(Path(self.state["worktrees_dir"]) / f"story-{story['key']}")
            try:
                found = implementation.story_add(self.repo, self.store, self.owner, implementation.StoryInput(
                    None, story["key"], self.state["branch"], worktree,
                    list(over.get("writable_scopes", defaults["writable_scopes"])),
                    list(over.get("project_clauses", defaults.get("project_clauses", []))),
                    epic_story={"epics_path": self.state["epics_path"], "story_key": story["key"]}))
            except DomainError as exc:
                return Outcome("failed", "story-add", {"key": story["key"], "gate": exc.code, "detail": exc.message})
            _hook("after-story-add", key=story["key"])
            feature_id, base_sha = found["feature_id"], found["base_commit"]
        self._advance(story["key"], stories=self._updated(story["number"], feature_id=feature_id, base_sha=base_sha))
        return self._current()

    def _lease(self, story: dict) -> dict:
        """E7/E8/E14/E15: the template filled for the story (its roles override, the feature's mandate revision),
        bound under the epic's owner and leased. The feature's newest anchored lease of this epic (a run killed between
        the bind and the story's record of it) is reused while its pipeline is at the feature's revision, re-sealed
        first under the owner's generation when an E12 re-claim came between; after a restart the revision moved, so
        a new pipeline is bound. A carry in flight never reaches here: its run never ended, so the story keeps its
        `lease_id` (ruling H11)."""
        self._restart_pending(story)
        snapshot = self.store.read_export_snapshot(self.owner)
        feature = next(row for row in snapshot["features"] if row["id"] == story["feature_id"])
        rows = [row for row in snapshot["events"] if row["kind"] == "drive.leased" and
                row["source_id"] == story["feature_id"]]
        lease = _sealed(self.base, rows[-1]) if rows else None
        current = False
        if lease is not None and lease.get("epic_id") == self.state["epic_id"]:
            value = drive_pipeline.load(self.base, lease["pipeline_sha256"])
            current = value["mandate_revision"] == feature["mandate_revision"]
        if current:
            lease = self._owned(lease)
        else:
            over = self.state["stories_input"].get(story["key"], {})   # E14: the story's own worker box
            pipeline = fill_template(self.template, story["feature_id"], story["key"], over.get("roles"),
                                     feature["mandate_revision"])
            drive_pipeline.check_bindable(snapshot, pipeline)
            lease = drive.bind_and_lease(self.repo, self.store, self.owner, pipeline, self.bounds,
                                         self.state["judgment_root"], EPIC_SESSION + self.state["epic_id"],
                                         epic_id=self.state["epic_id"])
        self._advance(story["key"], stories=self._updated(story["number"], lease_id=lease["lease_id"]))
        return self._current()

    def _remaining(self) -> RunBounds:
        """Design epic-drive E9: a story run's bounds: `--max-turns` per story run, the deadline what is left of this
        epic run's (counted from its start or resume)."""
        remaining = self.bounds.deadline_seconds - (self.clock() - self.started)
        return RunBounds(self.bounds.max_turns, max(0.0, remaining), self.bounds.human_wait_seconds,
                         self.bounds.poll_seconds)

    def _run(self, story: dict) -> Outcome | None:
        """E7/E10: the story driver's run (a start, or a resume of the lease's earlier run) in the story worktree with
        the remaining deadline; its outcome other than `done` ends the epic run with the story's reason. The lease runs
        under the owner's generation (E12)."""
        lease = self._owned(self._lease_record(story["lease_id"]))
        pipeline = drive_pipeline.load(self.base, lease["pipeline_sha256"])
        bounds = self._remaining()
        try:
            _bounds_check(pipeline, bounds)
        except Refusal:
            return Outcome("continue", "deadline", {"key": story["key"]})
        repo = implementation.resolve_repo(Path(self._feature(story)["worktree"]))
        started = any(row["kind"] == "drive.started" and row["subject_id"] == lease["lease_id"]
                      for row in self.store.read_snapshot()["events"])
        summary, _ = drive.Run(repo, self.store, self.owner, lease, pipeline, bounds, self.clock,
                               "resume" if started else "start", release_on_done=False).execute()
        self.counts["runs"] += 1
        self._advance(story["key"], stories=self._updated(story["number"], outcome=summary["outcome"],
                                                          reason=summary["reason"], run_id=summary["run_id"]))
        if summary["outcome"] != "done":
            self.story_outcome = True
            return Outcome(summary["outcome"], summary["reason"],
                           {**(summary.get("subjects") or {}), "key": story["key"], "feature_id": story["feature_id"],
                            "lease_id": lease["lease_id"], "run_id": summary["run_id"]})
        return None

    def _integrate(self, story: dict) -> Outcome | None:
        """E4/E9: the story branch merged into the integration branch; `already` after a kill between the merge and its
        record."""
        drift = self._check()
        if drift is not None:
            return drift
        branch = f"story/{story['key']}"
        result = merge_into(Path(self.state["worktree"]), f"refs/heads/{branch}",
                            f"epic({self.state['epic']}): merge {branch}", self.template["project"])
        if result["outcome"] == "conflict":
            return Outcome("needs-human", "integration-conflict", {
                "merge": "story", "ref": f"refs/heads/{branch}", "key": story["key"], "detail": result["detail"],
                "worktree": self.state["worktree"]})
        _hook("after-epic-merge", key=story["key"], outcome=result["outcome"])
        if result["outcome"] != "already":
            self.counts["merges"] += 1
        self._advance(story["key"], head_sha=result["head"],
                      stories=self._updated(story["number"], state="done", head_sha=result["head"]))
        self.counts["stories_done"] += 1
        return None

    def _stopped_story(self, key: str, verb: str) -> tuple[dict, dict | None]:
        """Ruling R2 and design epic-drive E15: the story the last stop named (current or blocked), to skip or
        restart, with the summary of its stopped run (None without a feature): without a feature at once; with one,
        only after a run that ended needs-human or failed and while no attempt of it holds the fence, never while a
        recorded restart has not run yet. A refusal's reason is `<verb>-refused`."""
        reason = f"{verb}-refused"
        story = next((item for item in self.state["stories"] if item.get("key") == key or item["number"] == key), None)
        if story is None or story["state"] not in ("current", "blocked"):
            raise Refusal("INVALID_INPUT", f"Story {key} is not one the last stop named", reason)
        last = None
        if story.get("feature_id") is not None:
            if story.get("lease_id") is None and story.get("restarts"):
                raise Refusal("INVALID_INPUT", f"Story {key} has a restart pending; epic resume runs it first", reason)
            last = self._last_summary(story["lease_id"]) if story.get("lease_id") else None
            if last is None or last.get("outcome") not in ("needs-human", "failed"):
                raise Refusal("INVALID_INPUT", f"Story {key} has a run that did not end needs-human or failed", reason)
            holder = drive_pipeline.fence_holder(self.store.read_export_snapshot(self.owner), story["feature_id"])
            if holder is not None:
                raise Refusal("BLOCKED_EVIDENCE", f"Story {key}: assignment {holder[0]} holds the fence ({holder[1]})",
                              reason,
                              f"worker reconcile {holder[0]} --token-file {TOKEN}, then " +
                              (f"epic resume --skip {key}" if verb == "skip" else
                               "epic resume (a restart follows the run's terminal stop)"))
        return story, last

    def skip(self, key: str) -> None:
        """Ruling R2: the named story becomes skipped (`_stopped_story`)."""
        story, _ = self._stopped_story(key, "skip")
        self._advance(f"skip:{story['number']}", stories=self._updated(story["number"], state="skipped"))
        self.counts["stories_skipped"] += 1

    def restart(self, key: str, roles: dict | None) -> None:
        """Design epic-drive E15 (a): the current story with a feature (`_stopped_story`) records in the next state
        revision its restart count `restarts` (n, after the feature's own `epic-restart:<n>` decisions, so a feature
        from an earlier epic never collides), the stopped run's evidence `restart_carry` (ruling H8) and the optional
        roles override, its `lease_id`, `outcome`, `reason` and `run_id` cleared. The story's effective box (the new
        override, else its recorded one) is checked like a start's (E14, ruling H6). The mandate revision follows at
        `_lease` (`_restart_pending`), so a kill in between is finished at the next resume."""
        story, last = self._stopped_story(key, "restart")
        if story.get("feature_id") is None or story["state"] != "current":
            raise Refusal("INVALID_INPUT", f"Story {key} has no feature to restart; epic resume continues it",
                          "restart-refused")
        if last.get("reason") not in RESTARTABLE:   # ruling H10: a stop with a designed answer is answered
            raise Refusal("INVALID_INPUT", f"Story {key} stopped {last.get('reason')}, not a terminal stop: epic "
                          "resume continues it (a decision stop takes an inbox decision or an accept first); --restart "
                          "follows a terminal stop", "restart-refused")
        over = self.state["stories_input"].get(story["key"], {})
        if roles is not None:
            over = {**over, "roles": _roles_override(roles, f"stories.{story['key']}")}
        _check_overrides(self.template, {story["key"]: over}, self.bounds)
        snapshot = self.store.read_export_snapshot(self.owner)
        numbers = [row["source_ref"].removeprefix(RESTART) for row in snapshot["decisions"]
                   if row["feature_id"] == story["feature_id"] and row["source_ref"].startswith(RESTART)]
        n = max((int(item) for item in numbers if item.isascii() and item.isdigit()), default=0) + 1
        carry = _restart_carry(last, snapshot, self.template.get("accept_on_high", False))
        changes = {"stories": self._updated(story["number"], lease_id=None, outcome=None, reason=None, run_id=None,
                                            restarts=n, restart_carry=carry)}
        if roles is not None:
            changes["stories_input"] = {**self.state["stories_input"], story["key"]: over}
        self._advance(f"restart:{story['number']}", **changes)
        _hook("after-restart-record", key=story["key"])
        self.counts["stories_restarted"] += 1

    def _restart_pending(self, story: dict) -> None:
        """Design epic-drive E15 (b), ruling H9: the story's recorded restart n decided by the feature's decision
        `epic-restart:<n>`, never by `constraints.restarts` (a later carry replaces the constraints): none, so an
        operator-reported decision is recorded at the feature's revision and the mandate revised; one at the
        feature's revision (a kill between the two), so the mandate is revised; one below it, so nothing is left. The
        revise sets `constraints.restarts` and the story's `restart_carry` (ruling H8), or drops an older carry."""
        n = story.get("restarts", 0)
        if n == 0:
            return
        snapshot = self.store.read_export_snapshot(self.owner)
        feature = next(row for row in snapshot["features"] if row["id"] == story["feature_id"])
        coordinator, revision, source = Coordinator(self.store), feature["mandate_revision"], f"{RESTART}{n}"
        found = next((row for row in snapshot["decisions"] if row["feature_id"] == feature["id"] and
                      row["source_ref"] == source), None)
        if found is not None and found["mandate_revision"] < revision:
            return
        decision = found["id"] if found is not None else coordinator.record_decision(self.owner, DecisionInput(
            feature["id"], revision, "operator-reported", source,
            f"the operator restarted story {story['key']} inside the epic (design epic-drive E15)", {"restart": n}))
        constraints = {name: value for name, value in json.loads(feature["constraints_json"]).items()
                       if name != "restart_carry"}
        constraints["restarts"] = n
        if story.get("restart_carry"):
            constraints["restart_carry"] = story["restart_carry"]
        coordinator.revise_mandate(self.owner, feature["id"], revision, {"constraints": constraints}, decision)
        _hook("after-restart-revise", key=story["key"])

    # The end ---------------------------------------------------------------------------------------------------
    def _end(self, outcome: Outcome) -> tuple[dict, int]:
        """Design epic-drive E10 (ruling R3): the epic summary, sealed as epics/<id>/summary-<n>.json and anchored
        `epic.ended`. The action follows the outcome's origin: a story run's outcome keeps the story's own text (with
        `epic resume`) and the epic options, even for a reason an epic-level stop shares (`worktree-drift`); the loop's
        own stops take the epic's text (`_epic_action`), and so do its own failures (`_loop_action`), never the story
        drive's `drive start`. At `epic-done` the session is released when D2 holds."""
        subjects = outcome.subjects or {}
        story = self._current()
        if self.story_outcome:
            action = drive._operator_action(outcome.outcome, outcome.reason, subjects, command="epic")
        elif outcome.reason in EPIC_REASONS:
            action = _epic_action(outcome.reason, subjects)
        else:
            action = _loop_action(outcome.reason, subjects, self.state["epic_id"]) or \
                drive._operator_action(outcome.outcome, outcome.reason, subjects, command="epic")
        stories = self.state["stories"]
        summary = {"kind": "epic-summary", "format": FORMAT, "epic_id": self.state["epic_id"], "run_id": self.run_id,
                   "generation": self.owner.generation, "outcome": outcome.outcome, "reason": outcome.reason,
                   "subjects": subjects,
                   "story": story and {key: story.get(key) for key in ("key", "number", "feature_id", "lease_id",
                                                                       "run_id", "outcome", "reason", "restarts")},
                   "epic": {**{key: self.state[key] for key in ("branch", "worktree", "head_sha", "base_branch",
                                                                "base_synced_sha")},
                            "stories": [{key: item.get(key) for key in ("number", "key", "state", "outcome",
                                                                        "head_sha", "restarts")} for item in stories]},
                   "operator_action": action,
                   "epic_action": _story_epic_action(outcome, story, self.state["epic_id"], self.story_outcome),
                   "counts": dict(self.counts),
                   "ownership": "unchanged" if outcome.outcome == "failed" else "retained", "ownership_reason": None,
                   "ended_at": _now()}
        if outcome.reason == "ownership-lost":
            return summary, EXIT[outcome.outcome]
        release = False
        try:
            if outcome.outcome == "done":
                blocked = drive._releasable(self.store.read_export_snapshot(self.owner))
                summary["ownership"], summary["ownership_reason"] = ("retained", blocked) if blocked else \
                    ("released", None)
                release = blocked is None
            ended = [row for row in self.store.read_snapshot()["events"] if row["kind"] == "epic.ended" and
                     row["subject_id"] == self.state["epic_id"]]
            path = _epic_dir(self.base, self.state["epic_id"]) / f"summary-{len(ended) + 1}.json"
            ref, digest = _seal_record(self.store, path, summary)
            Coordinator(self.store).anchor_event(self.owner, "epic.ended", self.state["epic_id"],
                                                 (self.run_id, ref, digest))
            if release:
                Coordinator(self.store).release(self.owner)
        except (DomainError, OSError) as exc:
            error = {"code": getattr(exc, "code", "OS_ERROR"), "message": getattr(exc, "message", str(exc))}
            summary = {**summary, "end_error": error}
            if release:
                summary.update(ownership="retained", ownership_reason=f"not released: {error['code']}")
        return summary, EXIT[outcome.outcome]


# Entry points (E1, E2, E11, E12, E13) ---------------------------------------------------------------------------------

def start(repo, spec: EpicInput, template, token_path: Path, bounds: RunBounds, judgment_root=None, *,
          clock=time.monotonic) -> tuple[dict, int]:
    """Design epic-drive E1/E2/E13: validate (nothing written on a refusal), lock, claim `epic:<id>`, create the
    integration branch and its worktree, seal the template and state 1, run."""
    store = open_store(repo)
    value = check_input(repo, spec)
    template = check_template(template)
    sample = fill_template(template, SAMPLE["__FEATURE__"], SAMPLE["__KEY__"])
    _bounds_check(sample, bounds)
    _check_overrides(template, value["stories"], bounds)
    root = drive._judgment_root(repo, sample, judgment_root)
    worktree = Path(value["worktrees_dir"]) / f"epic-{value['epic']}"
    if _rev(repo.worktree, f"refs/heads/{value['branch']}") is not None:
        raise Refusal("INVALID_INPUT", f"Branch {value['branch']} exists", "branch-exists",
                      "epic resume continues an epic; a new epic takes another branch name")
    if os.path.lexists(worktree):
        raise Refusal("INVALID_INPUT", "Epic worktree path exists", "worktree-exists")
    if _rev(repo.worktree, value["base"] if value["base"] is not None else "HEAD") is None:
        raise _invalid(f"base {value['base']} does not resolve to a commit")
    fd = drive.acquire_lock(repo, exclusive=True)
    try:
        epic_id = uuid.uuid4().hex
        owner = Coordinator(store).claim(EPIC_SESSION + epic_id, token_path)
        try:
            created = create_integration(repo, value["branch"], value["base"], worktree)
        except BaseException:
            Coordinator(store).release(owner)   # nothing of the epic exists: hand the repository back
            Path(token_path).unlink(missing_ok=True)
            raise
        _hook("after-epic-branch", branch=value["branch"])
        base = store.path.parent
        ref, digest = _seal(base, _epic_dir(base, epic_id) / "template.json", template)
        now = _now()
        state = {"kind": "epic-state", "format": FORMAT, "revision": 1, "epic_id": epic_id,
                 "repository_id": store.repository_id, "generation": owner.generation,
                 "epics_path": value["epics_path"], "epic": value["epic"], "branch": value["branch"],
                 "worktree": str(worktree), "worktrees_dir": value["worktrees_dir"],
                 "base_ref": value["base"] if value["base"] is not None else "HEAD",
                 "base_commit": created["base_commit"], "base_branch": created["base_branch"],
                 "base_synced_sha": created["base_commit"], "head_sha": created["base_commit"], "template_ref": ref,
                 "template_sha256": digest, "judgment_root": root, "bounds": asdict(bounds),
                 "story_defaults": value["story_defaults"], "stories_input": value["stories"], "stories": [],
                 "created_at": now, "advanced_at": now}
        anchor_state(store, owner, state, "epic.started", "epic")
        return Epic(repo, store, owner, state, template, bounds, clock).execute()
    finally:
        os.close(fd)


def resume(repo, owner: OwnerRef | None, bounds=None, judgment_root=None, *, skip: str | None = None,
           restart: str | None = None, roles: dict | None = None, epic_id: str | None = None,
           token_path: Path | None = None, clock=time.monotonic) -> tuple[dict, int]:
    """Design epic-drive E11 (and ruling R2): lock, the epic from the owner session, optional skip or restart (E15,
    with an optional roles override), a new run. E12: without an owner (no session owns the repository) `epic_id` and
    a fresh token file claim `epic:<id>` anew; the state and the current story's lease are re-sealed under the new
    generation."""
    if skip is not None and restart is not None:
        raise _invalid("--skip and --restart exclude each other")
    if roles is not None and restart is None:
        raise _invalid("--roles goes with --restart")
    store = open_store(repo)
    fd = drive.acquire_lock(repo, exclusive=True)
    try:
        if owner is None:
            if epic_id is None or token_path is None:
                raise Refusal("INVALID_INPUT", "epic resume without a session needs --epic and a token file to create",
                              "run-unbound", "epic resume --epic <id> --token-file <new file>")
            state = newest_state(store, epic_id)
            if state is None:
                raise Refusal("NOT_FOUND", "No such epic", "run-unbound", "epic status lists the epics")
            owner = Coordinator(store).claim(EPIC_SESSION + epic_id, token_path)
            state = {**state, "generation": owner.generation, "revision": state["revision"] + 1, "advanced_at": _now()}
            anchor_state(store, owner, state, "epic.advanced", "reclaim")
            current = next((item for item in state["stories"] if item["state"] == "current" and item.get("lease_id")),
                           None)
            if current is not None:
                rows = [row for row in store.read_snapshot()["events"] if row["kind"] == "drive.leased" and
                        row["subject_id"] == current["lease_id"]]
                drive.reseal_lease(store, owner, _sealed(store.path.parent, rows[-1]), f"lease-g{owner.generation}")
        else:
            store.read_export_snapshot(owner)   # the fence first: a stale or never-claimed token is OWNERSHIP_CONFLICT
            state = find_epic(store, owner)
        template = drive_pipeline.read_sealed_json(store.path.parent, state["template_ref"], state["template_sha256"])
        if not isinstance(bounds, RunBounds):
            bounds = RunBounds(**{**state["bounds"], **(bounds or {})})
        _bounds_check(fill_template(template, SAMPLE["__FEATURE__"], SAMPLE["__KEY__"]), bounds)
        named = {name for name in (skip, restart) if name is not None}
        finished = {item.get("key") for item in state["stories"]
                    if item["state"] in ("done", "skipped", "already-done") or
                    named & {item.get("key"), item["number"]}}   # ruling H6 (amended): restart checks its own box
        _check_overrides(template, state["stories_input"], bounds, finished)
        if judgment_root is not None and os.path.realpath(judgment_root) != state["judgment_root"]:
            raise Refusal("INVALID_INPUT", "Judgment root differs from the epic", "judgment-root-mismatch")
        epic = Epic(repo, store, owner, state, template, bounds, clock)
        if skip is not None:
            epic.skip(skip)
        if restart is not None:
            epic.restart(restart, roles)
        return epic.execute()
    finally:
        os.close(fd)


# Status (E13) --------------------------------------------------------------------------------------------------------

STATE_VIEW = ("revision", "generation", "epic", "epics_path", "branch", "worktree", "base_branch", "base_synced_sha",
              "head_sha", "stories")
SUMMARY_VIEW = ("run_id", "outcome", "reason", "operator_action", "epic_action", "ended_at")


def status(repo, epic_id: str | None = None) -> dict:
    """Design epic-drive E13: read only, creates nothing (no token, no lock, the store read as `drive status` reads
    it): every epic (newest first) with its newest state, its run count and its last summary; with `epic_id`, that
    epic alone plus `drive status` of its current story's feature."""
    try:
        store = open_store(repo)
    except DomainError as exc:
        if exc.code == "UNINITIALIZED":
            return {"state": "uninitialized"}
        raise
    snapshot = store.read_snapshot()
    ids = epic_ids(snapshot)
    if epic_id is not None:
        if epic_id not in ids:
            raise DomainError("NOT_FOUND", "No such epic")
        ids = [epic_id]
    epics = []
    for item in reversed(ids):
        state = newest_state(store, item, snapshot)
        ends = [row for row in snapshot["events"] if row["kind"] == "epic.ended" and row["subject_id"] == item]
        summary = _sealed(store.path.parent, ends[-1]) if ends else None
        current = next((story for story in (state or {}).get("stories", []) if story["state"] == "current"), None)
        view = {"epic_id": item, "state": state and {key: state.get(key) for key in STATE_VIEW}, "runs": len(ends),
                "last_summary": summary and {key: summary.get(key) for key in SUMMARY_VIEW}, "current_story": None}
        if epic_id is not None and current is not None and current.get("feature_id"):
            view["current_story"] = drive.status(repo, current["feature_id"])
        epics.append(view)
    return {"repository_id": snapshot["repository_id"], "owner": snapshot["owner"], "lock": drive.lock_state(repo),
            "epics": epics}
