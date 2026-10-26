"""Unattended continuation driver (docs/unattended-driver-design.md): an explicitly started, bounded, foreground
process that holds the run lock, owns one feature by orderly handoff and runs its persisted pipeline turn by turn:
the no-progress rule, R3 stop (a stop outranks every bounds outcome), R1 max-turns and deadline, R2 fence read and
mandate check (a pending stop therefore also outranks mandate-revised; both exit 11), R4 reconcile, R4j judgment
classification, R5 inbox, R6 derive, R7 at most one action (prepare, launch, judge, decide, cancel-stale, the
slice-3 wrappers bundle, review-prepare, review-record, accept and export, and the story actions setup, commit, verify,
status, decide-form and carry), W record. Outcomes are exactly continue, needs-human, done and failed. Linux only:
fcntl.flock and /proc."""

import fcntl
import hashlib
import json
import os
import secrets
import signal
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import bmad_implementation as implementation
from . import bill, bmad_planning, bmad_review, bundles, clients, drive_pipeline, exporter, inbox, judgment, worker
from . import bmad_spec_author
from .drive_pipeline import open_high_findings
from .model import DecisionInput, DomainError, OwnerRef
from .run_receipt import canonical, seal
from .service import DRIVE_EVENTS, HOLDING_STATES, Coordinator, releasable, require_open_feature
from .store import open_store

HOOK = None   # test seam: callable(point, **info) at named points; production leaves it None
LOCK = "drive.lock"
SESSION = "drive:"
EPIC_SESSION = "epic:"   # design epic-drive E1: the owner session of an epic run
MARGIN = 120
EXIT = {"done": 0, "continue": 10, "needs-human": 11, "failed": 12}
# Design section 6: the question and judgment codes (update-budget aside) are answered by an inbox decision for R.
REQUEST_REASONS = ("question-conflict", "judgment-budget", "question-refused", "judgment-unknown",
                   "judgment-preflight", "product-decision", "spec-decisions")
STOP_REASONS = ("operator-stop", "signal-stop")
GATE_REASONS = {"UNSUPPORTED_CONTRACT": "compat-review"}
TOKEN = "<driver token>"
_sleep = time.sleep   # the human-wait poll interval; a test seam


def _hook(point: str, **info) -> None:
    if HOOK is not None:
        HOOK(point, **info)


class DriveAbort(BaseException):
    """Raised from the signal handler on a second SIGINT, SIGTERM or SIGHUP; run_assignment's BaseException path
    stops the process group and the attempt stays holding (unknown)."""


class Refusal(DomainError):
    """A pre-run refusal with a closed reason code and the operator action; writes nothing."""

    def __init__(self, code: str, message: str, reason: str, action: str | None = None):
        super().__init__(code, message)
        self.extra = {"reason": reason, **({"action": action} if action else {})}


@dataclass(frozen=True)
class RunBounds:
    max_turns: int
    deadline_seconds: float
    human_wait_seconds: float = 0
    poll_seconds: float = 5.0


# Lock ---------------------------------------------------------------------------------------------------------------

def acquire_lock(repo, exclusive: bool) -> int:
    """<common-dir>/coordinator/drive.lock, opened O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC (0600) and never unlinked:
    LOCK_EX|LOCK_NB for a driver, LOCK_SH|LOCK_NB for every owner-fenced CLI command. The kernel drops a dead
    holder's lock; the lock is advisory, so the Python API stays unguarded."""
    directory = repo.common_dir / "coordinator"
    if directory.is_symlink() or not directory.is_dir():
        raise DomainError("UNSAFE_PATH", "Coordinator directory is missing or a symlink")
    try:
        fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise DomainError("UNSAFE_PATH", "Coordinator drive lock is not a private regular file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise DomainError("UNSAFE_PATH", "Coordinator drive lock is not a private regular file")
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise DomainError("OWNERSHIP_CONFLICT", "Coordinator drive lock is held by another process") from exc
    except OSError as exc:
        os.close(fd)
        raise DomainError("UNSAFE_PATH", "Coordinator drive lock cannot be inspected or locked") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def lock_state(repo) -> dict:
    """Whether some local process holds drive.lock, read from /proc/locks; never acquires the lock."""
    try:
        info = os.lstat(repo.common_dir / "coordinator" / LOCK)
        lines = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return {"held": False, "mode": None, "pids": []}
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    held = [line.split() for line in lines if "->" not in line]
    held = [fields for fields in held if len(fields) > 5 and fields[1] == "FLOCK" and fields[5] == device]
    if not held:
        return {"held": False, "mode": None, "pids": []}
    return {"held": True, "mode": "exclusive" if any(fields[3] == "WRITE" for fields in held) else "shared",
            "pids": sorted({int(fields[4]) for fields in held if fields[4].isdigit()})}


# Sealed driver files ------------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _directory(path: Path) -> Path:
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise DomainError("UNSAFE_PATH", "Driver record directory is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _seal(base: Path, path: Path, value: dict) -> tuple[str, str]:
    _directory(path.parent)
    digest = seal(path, value)
    return path.relative_to(base).as_posix(), digest


def _sealed(base: Path, row) -> dict | None:
    try:
        return drive_pipeline.read_sealed_json(base, row["evidence_ref"], row["payload_digest"])
    except (DomainError, ValueError, TypeError):
        return None


def _bounds_check(pipeline: dict, bounds: RunBounds) -> None:
    for name, value in (("max_turns", bounds.max_turns), ("deadline_seconds", bounds.deadline_seconds),
                        ("human_wait_seconds", bounds.human_wait_seconds), ("poll_seconds", bounds.poll_seconds)):
        if type(value) not in (int, float) or value < 0 or (name in ("max_turns", "poll_seconds") and value <= 0):
            raise DomainError("INVALID_INPUT", f"Run bound {name} is invalid")
    largest = max(role["timeout"] for role in pipeline["roles"].values())
    if bounds.deadline_seconds < largest + MARGIN:
        raise Refusal("INVALID_INPUT", f"Deadline must be at least the largest role timeout plus {MARGIN} s "
                      f"({largest + MARGIN} s)", "bounds-insufficient")


def _judgment_root(repo, pipeline: dict, value) -> str | None:
    """With max_judgments > 0 the root is required and validated with run_isolated's rule before anything is written
    (judgment.validate_root); otherwise an optional existing directory, as before."""
    if pipeline["budgets"]["max_judgments"] > 0:
        if value is None:
            raise Refusal("INVALID_INPUT", "--judgment-root is required when max_judgments > 0",
                          "judgment-root-required")
        return judgment.validate_root(repo, value)["root"]
    if value is None:
        return None
    root = os.path.realpath(value)
    if not os.path.isdir(root):
        raise DomainError("INVALID_INPUT", "Judgment root must be an existing directory")
    return root


def _releasable(snapshot: dict) -> str | None:
    """None when D2 holds (service.releasable), else the reason ownership is retained."""
    row = next((row for row in snapshot["assignments"] if not releasable(row["state"], row["return_json"])), None)
    return None if row is None else f"assignment {row['id']} is {row['state']} without an observed terminal return"


# A story stop whose attempt is final continues only at a new mandate revision (a pipeline is bound once per revision).
_NEW_REVISION = (f"with the driver's token while no driver runs, revise the mandate, "
                 f"release --token-file {TOKEN}, then drive start a pipeline bound to the new revision")


def _drive_action(outcome: str, reason: str | None, subjects: dict) -> str | None:
    if outcome == "done":
        return None
    if outcome == "continue":
        return "drive resume"
    if reason == "ownership-lost":
        return "none: another owner holds the feature"
    holder = subjects.get("holder") or subjects.get("assignment_id")
    if reason == "stale-input" and "next_judgment_key" in subjects:  # cited bytes differ from the observed return
        return "restore the cited file to its observed bytes and run `drive resume`"
    if reason == "judgment-preflight" and "next_judgment_key" in subjects:  # refused before any reservation
        return (f"fix the judgment root or client, then drive resume (nothing was reserved, so the judgment is "
                f"retried), or inbox add decision for request {subjects.get('request_id')}")
    if reason == "judgment-preflight":
        return (f"fix the judgment client or root; a failed-preflight judgment is never replaced, so inbox add "
                f"decision for request {subjects.get('request_id')}, then drive resume")
    if reason == "verify-env":   # the project block is sealed with the pipeline
        return (f"fix the environment so the setup and baseline commands pass (failed: "
                f"{', '.join(subjects.get('failed') or [])}), then drive resume (setup and baseline are re-run); "
                f"changing the commands needs a pipeline bound to a new mandate revision: release --token-file {TOKEN} "
                "first")
    if reason == "worktree-drift":
        expected = (subjects.get("expected") or "?")[:12]
        return (f"restore the story branch: inspect it (HEAD is expected on the story branch at {expected}; the "
                "detail says what differs), switch back to the story branch if HEAD left it, run `git reset --mixed "
                f"{expected}` if HEAD moved (a mixed reset keeps the working tree; a hard reset destroys the worker's "
                "uncommitted attempt) and remove stray changes in the writable scopes after an attempt commit, then "
                f"drive resume; or release --token-file {TOKEN} and finish the story attended")
    if reason == "status-shape":   # the status step needs HEAD at the base or the accepted commit and commits itself
        return (f"fix the project status file ({subjects.get('detail')}) in the story worktree without committing "
                "it, then drive resume (the status step edits and commits it); or release --token-file "
                f"{TOKEN} and finish the story attended")
    if reason == "client-failure":   # design D29
        return (f"the worker of assignment {subjects.get('assignment_id')} ended without a return "
                f"{subjects.get('failures')} times in a row (timeouts or crashes; the attempts are committed): check "
                "the client (login, quota, the model, the role's timeout), then drive resume to try once more; or "
                "raise budgets.client_failures in a pipeline bound to a new mandate revision (release --token-file "
                f"{TOKEN} first); or release and finish the story attended")
    if reason == "correction-budget":
        return ("the red verify receipt of the attempt commit is final, so a plain drive resume stops here again: fix "
                "the failing verification by hand in the story worktree and commit nothing yourself; then " +
                _NEW_REVISION + " (its corrections count starts at zero; raise max_corrections there if needed): the "
                "coordinator commits the writable scopes, so the next attempt's commit takes your fix; or release "
                f"--token-file {TOKEN} and finish the story attended")
    if reason == "worker-blocked" and subjects.get("condition"):   # a story attempt; planning keeps the default
        return (f"the attempt of assignment {subjects.get('assignment_id')} is final ({subjects['condition']}), so a "
                "plain drive resume stops here again: fix the condition in the story worktree or the brief inputs; "
                "then " + _NEW_REVISION + " (its first attempt is a fresh create); or release "
                f"--token-file {TOKEN} and finish the story attended")
    if reason == "spec-decisions" and subjects.get("request_id"):   # design D24: one decision answers every question
        return (f"inbox add decision for request {subjects.get('request_id')} (choice {{\"answer\": <text>}} answering "
                "every question, or {\"recommendations\": true}), then drive resume")
    if reason == "spec-decisions":   # rulings P6, P13: the human approves the spec (the mandate) or sends it back
        bundle = subjects.get("bundle_id")
        revise = (f'{{"feature_id": "{subjects.get("feature_id")}", '
                  f'"mandate_revision": {subjects.get("mandate_revision")}, "source_kind": "operator-reported", '
                  f'"source_ref": "spec-revise:{bundle}", "reason": <why>, "choice": {{"revise": <the changes>}}}}')
        return ("read the digest and the clauses (they become the story's delegated authority), then bmad accept "
                f"review {subjects.get('review_id')} of bundle {bundle} with the driver's token while no driver runs, "
                f"then drive resume; or, to have the author revise it, decision add --token-file {TOKEN} with {revise} "
                f"while no driver runs, then drive resume; or release --token-file {TOKEN} and revise the spec "
                "attended (spec_auto_accept: true in the pipeline skips this stop)")
    if reason == "cost-budget":   # bill design B5/B6: reported money only; the epic action names the epic commands
        missing = subjects.get("unreported_launches") or 0
        note = f" ({missing} launch{'' if missing == 1 else 'es'} without a figure)" if missing else ""
        if subjects.get("scope") == "epic":
            return (f"the epic's reported cost USD {subjects.get('used')} reached its budgets.max_cost_usd "
                    f"{subjects.get('max')}{note}, so a plain drive resume stops here again: raise the epic's bound "
                    f"or leave the story (the epic action names the commands); or release --token-file {TOKEN} and "
                    "finish the story attended")
        return (f"the story's reported cost USD {subjects.get('used')} reached budgets.max_cost_usd "
                f"{subjects.get('max')}{note}, so a plain drive resume stops here again: inside an epic, the epic "
                "action names the restart under a wider bound; for a story drive, " + _NEW_REVISION +
                f" with a wider max_cost_usd; or release --token-file {TOKEN} and finish the story attended")
    if reason in REQUEST_REASONS:
        return (f"inbox add decision for request {subjects.get('request_id')}, then drive resume "
                "(or keep a driver waiting with --human-wait)")
    if reason in ("attempt-unknown", "signal-abort", "unobserved-return", "fenced-by-other") and holder:
        return (f"worker reconcile {holder} --token-file {TOKEN}, investigate, and only if needed "
                f"attempt resolve {holder} --confirm-unknown --token-file {TOKEN}; then drive resume")
    if reason == "signal-abort":
        return "drive resume"
    if reason == "attempt-resolved":
        return (f"bmad prepare a new create or update attempt for step {subjects.get('step_id')} with the driver's "
                "token while no driver runs, then drive resume; or drop the step or feature by revising the mandate "
                "and binding a new pipeline (a new create needs an empty workspace; clearing it is the operator's "
                "decision)")
    if reason == "open-high-findings":  # one recovery path, mirroring attempt-resolved (followups review m3)
        return (f"bmad accept review {subjects.get('review_id')} of bundle {subjects.get('bundle_id')} (it lists the "
                "open high findings) with the driver's token while no driver runs, then drive resume; or release "
                f"--token-file {TOKEN}, an attended correction and re-review of bundle {subjects.get('bundle_id')}, "
                "then drive start")
    if reason == "mandate-revised":
        return f"release --token-file {TOKEN}, then drive start with a new pipeline bound to the revised mandate"
    if reason in STOP_REASONS:
        return f"drive resume to continue, or release --token-file {TOKEN} to hand back"
    return f"release --token-file {TOKEN}, attended repair, then drive start"


def _operator_action(outcome: str, reason: str | None, subjects: dict, command: str = "drive") -> str | None:
    """The story drive's operator action; inside an epic (design epic-drive E10, ruling R3) every `drive resume` reads
    `epic resume` and the rest of the story's text stands."""
    text = _drive_action(outcome, reason, subjects)
    return text if text is None or command == "drive" else text.replace("drive resume", f"{command} resume")


def _drifted(exc: DomainError) -> bool:
    """The module's refusal when HEAD moved under the driver (design catalogue: `worktree-drift`)."""
    return exc.code == "STALE_INPUT" and exc.message.startswith("worktree-drift")


def _drift(step, exc: DomainError, expected: str | None, assignment_id: str | None = None) -> "Outcome":
    subjects = {"step_id": step.step_id, "detail": exc.message, "expected": expected}
    if assignment_id:
        subjects["assignment_id"] = assignment_id
    return Outcome("needs-human", "worktree-drift", subjects)


# Entry points -------------------------------------------------------------------------------------------------------

def start(repo, feature_id: str, value: dict, token_path: Path, bounds: RunBounds, judgment_root=None, *,
          clock=time.monotonic) -> tuple[dict, int]:
    """Validate (writes nothing on refusal), then P0 lock, P1 claim as drive:<lease>, P2 bind and lease, P3 run."""
    store = open_store(repo)
    pipeline = drive_pipeline.validate(value)
    if pipeline["feature_id"] != feature_id:
        raise DomainError("INVALID_INPUT", "Pipeline is bound to another feature")
    _bounds_check(pipeline, bounds)
    root = _judgment_root(repo, pipeline, judgment_root)
    drive_pipeline.check_read_reviews(pipeline)   # Copilot reviewer design V8: nothing bound on a refusal
    drive_pipeline.check_bindable(store.read_snapshot(), pipeline)
    fd = acquire_lock(repo, exclusive=True)
    try:
        require_open_feature(store.read_snapshot(), feature_id)   # lifecycle F5: under the lock, before the claim
        lease_id = uuid.uuid4().hex
        owner = Coordinator(store).claim(SESSION + lease_id, token_path)
        _hook("after-claim", lease_id=lease_id)
        lease = bind_and_lease(repo, store, owner, pipeline, bounds, root, SESSION + lease_id, lease_id=lease_id)
        return Run(repo, store, owner, lease, pipeline, bounds, clock, "start").execute()
    finally:
        os.close(fd)


def bind_and_lease(repo, store, owner: OwnerRef, pipeline: dict, bounds: RunBounds, judgment_root, session_id: str, *,
                   lease_id: str | None = None, epic_id: str | None = None) -> dict:
    """P2 of a start: bind the pipeline, then seal and anchor the lease of this run's session (`drive:<lease>` for a
    story drive; `epic:<id>` for a story inside an epic, design epic-drive E1/E7, which also records `epic_id`)."""
    lease_id = lease_id or uuid.uuid4().hex
    bound = drive_pipeline.bind(repo, store, owner, pipeline, sealed=lambda: _hook("after-pipeline-seal"))
    _hook("after-bind", sha256=bound["sha256"])
    base = store.path.parent
    lease = {"kind": "drive-lease", "format": 1, "lease_id": lease_id, "repository_id": store.repository_id,
             "feature_id": pipeline["feature_id"], "pipeline_sha256": bound["sha256"], "pipeline_ref": bound["ref"],
             "generation": owner.generation, "session_id": session_id, "bounds": asdict(bounds),
             "judgment_root": judgment_root, "accept_on_high": pipeline.get("accept_on_high", False),
             "created_at": _now(), **({"epic_id": epic_id} if epic_id else {})}
    ref, digest = _seal(base, base / "drives" / lease_id / "lease.json", lease)
    Coordinator(store).anchor_event(owner, "drive.leased", lease_id, (pipeline["feature_id"], ref, digest))
    _hook("after-lease", lease_id=lease_id)
    return lease


def reseal_lease(store, owner: OwnerRef, lease: dict, name: str) -> dict:
    """Design epic-drive E12: the lease under the owner's generation (after a re-claim), sealed as
    drives/<lease>/<name>.json and anchored as a further `drive.leased` of the lease id; identical bytes sealed before a
    kill are anchored as they are (the carry's rule)."""
    resealed = {**lease, "generation": owner.generation}
    base = store.path.parent
    path = base / "drives" / lease["lease_id"] / f"{name}.json"
    try:
        ref, digest = _seal(base, path, resealed)
    except FileExistsError:
        ref, digest = path.relative_to(base).as_posix(), hashlib.sha256(canonical(resealed)).hexdigest()
        drive_pipeline._read_sealed(base, ref, digest)
    Coordinator(store).anchor_event(owner, "drive.leased", lease["lease_id"], (lease["feature_id"], ref, digest))
    return resealed


def find_lease(store, owner: OwnerRef) -> dict:
    """The lease of the owner session drive:<lease-id>, read from its anchored lease.json, or run-unbound."""
    action = f"release --token-file {TOKEN}, then drive start"
    session = store.read_snapshot()["owner"]["session_id"] or ""
    if session.startswith(EPIC_SESSION):   # design epic-drive E11: an epic's session continues through `epic resume`
        raise Refusal("INVALID_INPUT", "The owner session is an epic run", "epic-session",
                      f"epic resume --token-file {TOKEN}")
    events = store.read_export_snapshot(owner)["events"]
    lease_id = session[len(SESSION):] if session.startswith(SESSION) else None
    rows = [row for row in events if row["kind"] == "drive.leased" and row["subject_id"] == lease_id]
    lease = _sealed(store.path.parent, rows[-1]) if rows else None
    if lease is None or lease.get("lease_id") != lease_id or lease.get("generation") != owner.generation:
        raise Refusal("RECOVERY_BLOCKED", "The owner session has no anchored drive lease", "run-unbound", action)
    return lease


def resume(repo, owner: OwnerRef, bounds: RunBounds | dict | None = None, judgment_root=None, *,
           clock=time.monotonic) -> tuple[dict, int]:
    """P0 lock, then the lease from the owner session, the bounds (a dict overrides the lease's start bounds) against
    the lease's pipeline and the sealed judgment root; then a new run of the same lease and generation."""
    store = open_store(repo)
    fd = acquire_lock(repo, exclusive=True)
    try:
        snapshot = store.read_export_snapshot(owner)  # fence first: a stale or unclaimed token is OWNERSHIP_CONFLICT
        lease = find_lease(store, owner)
        require_open_feature(snapshot, lease["feature_id"])   # feature lifecycle F5: before the run's first write
        pipeline = drive_pipeline.load(store.path.parent, lease["pipeline_sha256"])
        if not isinstance(bounds, RunBounds):
            bounds = RunBounds(**{**lease["bounds"], **(bounds or {})})
        _bounds_check(pipeline, bounds)
        if judgment_root is not None and os.path.realpath(judgment_root) != lease["judgment_root"]:
            raise Refusal("INVALID_INPUT", "Judgment root differs from the lease", "judgment-root-mismatch")
        return Run(repo, store, owner, lease, pipeline, bounds, clock, "resume").execute()
    finally:
        os.close(fd)


class Launcher:
    """Launches one attempt for a pipeline role through the closed client registry; never names a client itself."""

    def __init__(self, repo, store, owner: OwnerRef, pipeline: dict):
        self.repo, self.store, self.owner, self.pipeline = repo, store, owner, pipeline

    def launch(self, assignment_id: str, role: str) -> worker.RunReport:
        spec = self.pipeline["roles"][role]
        client = clients.get(spec["client"])
        return worker.run_assignment(self.repo, self.store, self.owner, assignment_id, spec["model"],
                                     spec["timeout"], client.name, spec.get("max_turns"),   # design D25
                                     spec.get("boundary"))   # launch-boundary L1


@dataclass
class Outcome:
    outcome: str
    reason: str | None = None
    subjects: dict | None = None


class Run:
    def __init__(self, repo, store, owner, lease, pipeline, bounds, clock, mode, *, release_on_done=True,
                 epic_bound=None):
        self.repo, self.store, self.owner, self.lease = repo, store, owner, lease
        self.epic_bound = epic_bound   # bill design B6: (max_cost_usd, the epic's feature ids), from the epic driver
        self.pipeline, self.bounds, self.clock, self.mode = pipeline, bounds, clock, mode
        self.base = store.path.parent
        self.lease_id, self.feature_id = lease["lease_id"], lease["feature_id"]
        self.started = clock()
        self.turns, self.no_progress = 0, 0
        self.reconciled, self.steps, self.decisions, self.interventions = set(), [], [], []
        self.launches, self.acknowledged = {}, []
        self.corrections = 0
        self.commits = self.verifications = self.carries = 0
        self.graceful = self.aborting = self.ending = False
        self.attempt = self.judging = None
        self.run_id, self.restarts = None, 0
        self.release_on_done = release_on_done

    # Signals ---------------------------------------------------------------------------------------------------
    def _on_signal(self, signum, frame):
        name = signal.Signals(signum).name
        if self.ending:  # never raised once ending; a first SIGINT still turns a continue outcome into signal-stop
            if signum == signal.SIGINT and not self.graceful and not self.aborting:
                self.graceful = True
                self.interventions.append({"type": "signal", "signal": name, "effect": "graceful"})
            return
        if self.aborting:
            return
        if signum == signal.SIGINT and not self.graceful:
            self.graceful = True
            self.interventions.append({"type": "signal", "signal": name, "effect": "graceful"})
            _hook("signal", signal=name, effect="graceful")
            return
        self.aborting = True
        self.interventions.append({"type": "signal", "signal": name, "effect": "abort"})
        _hook("signal", signal=name, effect="abort")
        raise DriveAbort(name)

    @contextmanager
    def _signals(self):
        previous = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, self._on_signal)
        except ValueError:  # not the main thread: no handlers, the caller owns interruption
            pass
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    # Run -------------------------------------------------------------------------------------------------------
    def execute(self) -> tuple[dict, int]:
        with self._signals():
            try:
                try:
                    outcome = self._run()
                except DriveAbort:  # raised at most once (aborting is set first), wherever it lands in _run
                    outcome = Outcome("needs-human", "signal-abort", {"holder": self.attempt} if self.attempt else
                                      {"judgment_key": self.judging} if self.judging else {})
            finally:
                self.ending = True  # from here on a signal is recorded, never raised
            return self._end(outcome)

    def _run(self) -> "Outcome":
        try:
            self._begin()
            _hook("run-started", run_id=self.run_id)
            # R4j once at run start too, so a killed judgment's window closes even when R3 or R2 ends the run
            judgment.reconcile(self.repo, self.store, self.owner, self.feature_id, self.lease["judgment_root"])
            return self._loop()
        except DomainError as exc:
            return self._failure(exc)
        except Exception as exc:  # noqa: BLE001 - every other failure ends the run honestly
            return Outcome("failed", "internal-error", {"detail": f"{type(exc).__name__}: {exc}"[:300]})

    def _elapsed(self) -> float:
        return self.clock() - self.started

    def _anchor(self, kind: str, path: Path, value: dict) -> None:
        ref, digest = _seal(self.base, path, value)
        Coordinator(self.store).anchor_event(self.owner, kind, self.lease_id, (self.run_id, ref, digest))

    def _begin(self) -> None:
        snapshot = self.store.read_export_snapshot(self.owner)
        events = snapshot["events"]
        prior = [row["source_id"] for row in events if row["kind"] == "drive.started" and
                 row["subject_id"] == self.lease_id]
        ends = {row["source_id"]: row for row in events if row["kind"] in ("drive.ended", "drive.stop-acknowledged")
                and row["subject_id"] == self.lease_id}
        self.restarts = len(prior)
        predecessor = {"run_id": prior[-1], "ended": prior[-1] in ends} if prior else None
        if prior and prior[-1] in ends and (_sealed(self.base, ends[prior[-1]]) or {}).get("outcome") == "needs-human":
            self.interventions.append({"type": "resume-after-needs-human", "run_id": prior[-1]})
        self.interventions += self._between_runs(snapshot)
        self.run_id = f"run-{len(prior) + 1:03d}-{secrets.token_hex(3)}"
        self.dir = self.base / "drives" / self.lease_id / self.run_id
        record = {"kind": "drive-run", "format": 1, "lease_id": self.lease_id, "run_id": self.run_id,
                  "feature_id": self.feature_id, "mode": self.mode, "pid": os.getpid(),
                  "generation": self.owner.generation, "bounds": asdict(self.bounds), "predecessor": predecessor,
                  "judgment_root": self.lease["judgment_root"], "warnings": self._root_warnings(),
                  "started_at": _now()}
        self._anchor("drive.started", self.dir / "run.json", record)

    def _root_warnings(self) -> list[str]:
        root = self.lease["judgment_root"]
        return ["judgment-root-in-repository"] if root and os.path.isdir(root) and judgment.in_repository(root) else []

    def _between_runs(self, snapshot: dict) -> list[dict]:
        """Typed interventions since the feature's last driver activity: attended generations (claims other than
        this lease's) and operator attempt resolutions."""
        events = snapshot["events"]
        leases = {row["subject_id"] for row in events if row["kind"] == "drive.leased" and
                  row["source_id"] == self.feature_id}
        marks = [row["revision"] for row in events if row["subject_id"] in leases and
                 row["kind"] in ("drive.started", "drive.turn", "drive.ended", "drive.stop-acknowledged")]
        if not marks:
            return []
        since = max(marks)
        mine = {row["id"] for row in snapshot["assignments"] if row["feature_id"] == self.feature_id}
        found = [{"type": "attended-generation", "generation": int(row["subject_id"])} for row in events
                 if row["kind"] == "owner.claimed" and row["revision"] > since and
                 row["subject_id"] != str(self.owner.generation)]
        found += [{"type": "attempt-resolve", "assignment_id": row["subject_id"]} for row in events
                  if row["kind"] == "assignment.resolved-unknown" and row["revision"] > since and
                  row["subject_id"] in mine]
        return found

    def _loop(self) -> Outcome:
        while True:
            sha = self.lease["pipeline_sha256"]   # a carry (design D24) moves the run to the carried pipeline
            # R1 no-progress, then R3 stop (ruling I1: a stop request outranks every bounds outcome, so a stopped run
            # never exits continue and the drive-loop never restarts it), then R1 bounds
            if self.no_progress >= 2:
                return Outcome("failed", "no-progress")
            stop = self._stop()
            if stop is not None:
                return stop
            if self.turns >= self.bounds.max_turns:
                return Outcome("continue", "max-turns")
            if self._elapsed() >= self.bounds.deadline_seconds:
                return Outcome("continue", "deadline")
            # R2 fence read
            snapshot = self.store.read_export_snapshot(self.owner)
            before = snapshot["revision"]
            feature = next(row for row in snapshot["features"] if row["id"] == self.feature_id)
            if feature["mandate_revision"] != self.pipeline["mandate_revision"]:
                status_sha = drive_pipeline.carried_from(snapshot, self.pipeline)
                if status_sha is None:
                    return Outcome("needs-human", "mandate-revised", {"feature_id": self.feature_id})
                self._carried(drive_pipeline.carry(self.repo, self.store, self.owner, self.lease, self.pipeline,
                                                   feature, status_sha, hook=_hook))   # killed after the revise
                continue
            # R4 reconcile, once per run for each holding attempt of the feature
            for row in snapshot["assignments"]:
                if row["feature_id"] == self.feature_id and row["state"] in HOLDING_STATES:
                    if row["id"] not in self.reconciled:
                        self.reconciled.add(row["id"])
                        report = worker.reconcile_run(self.repo, self.store, self.owner, row["id"])
                        if report.state != "unknown":
                            continue
                    return Outcome("needs-human", "attempt-unknown", {"holder": row["id"]})
            # R4j judgments: a reserved key without a terminal anchor is classified from anchors and its launch
            # intent (never re-launched once launched); orphan scratch of this store is removed. This closes the
            # judgment windows before R5.
            classified = judgment.reconcile(self.repo, self.store, self.owner, self.feature_id,
                                            self.lease["judgment_root"])
            # R5 inbox decisions (outside every window)
            consumed = self._consume()
            # R6 derive
            snapshot = self.store.read_export_snapshot(self.owner)
            facts = drive_pipeline.gather(self.repo, self.store, snapshot, self.pipeline, self.owner.generation)
            self.steps = drive_pipeline.derive(self.pipeline, sha, snapshot, facts)
            # R7 one action or an outcome
            current = next((step for step in self.steps if step.state != "satisfied"), None)
            if current is None:
                return Outcome("done")
            if current.state != "ready":
                outcome = Outcome("needs-human", current.reason, dict(current.subjects))
                # ruling P10: the spec approval stop (no request) is not an inbox question, and its accept needs a
                # stopped driver
                answerable = outcome.reason != "spec-decisions" or outcome.subjects.get("request_id")
                if outcome.reason in REQUEST_REASONS and answerable and self.bounds.human_wait_seconds > 0:
                    outcome = self._human_wait(outcome)
                    if outcome is None:
                        continue
                return outcome
            if current.action["kind"] in ("prepare", "launch", "review-prepare"):
                holder = drive_pipeline.fence_holder(snapshot, self.feature_id, current.action.get("assignment_id"))
                if holder is not None:
                    return Outcome("needs-human", holder[1], {"step_id": current.step_id, "holder": holder[0]})
            result = self._act(current)
            if isinstance(result, Outcome):
                return result
            record, moved = result  # moved: a launch, or an export pointer (which commits no event)
            _hook("after-action", turn=self.turns + 1, action=record["kind"])
            # W record
            self.turns += 1
            after = self.store.read_export_snapshot(self.owner)
            progress = moved or any(row["revision"] > before and row["kind"] not in DRIVE_EVENTS
                                    for row in after["events"])
            self.no_progress = 0 if progress else self.no_progress + 1
            self._anchor("drive.turn", self.dir / f"turn-{self.turns}.json", {
                "kind": "drive-turn", "format": 1, "lease_id": self.lease_id, "run_id": self.run_id,
                "turn": self.turns, "step_id": current.step_id, "action": record, "inbox": consumed,
                "judgments": classified,
                "reconciled": sorted(self.reconciled), "progress": progress, "revision_before": before,
                "revision_after": after["revision"], "steps": [step.view() for step in self.steps], "at": _now()})
            _hook("after-turn-event", turn=self.turns)

    def _stop(self) -> Outcome | None:
        found = inbox.stops(self.repo, self.store, self.owner, self.feature_id, self.owner.generation, self.dir)
        self.acknowledged = found["pending"]
        if not self.graceful and not found["pending"]:
            return None
        _hook("after-stop-seen", stops=found["pending"])
        self.interventions += [{"type": "inbox-stop", "name": name} for name in found["pending"]]
        return Outcome("needs-human", "operator-stop" if found["pending"] else "signal-stop",
                       {"stops": found["pending"]})

    def _consume(self) -> dict:
        result = inbox.consume_decisions(self.repo, self.store, self.owner, self.feature_id, self.dir)
        for item in result["consumed"]:
            row = next(row for row in self.store.read_export_snapshot(self.owner)["decisions"]
                       if row["id"] == item["decision_id"])
            self.decisions.append({"id": row["id"], "source_kind": row["source_kind"],
                                   "source_ref": row["source_ref"], "request_id": row["request_id"]})
            self.interventions.append({"type": "inbox-decision", "name": item["name"],
                                       "request_id": item["request_id"]})
        return {"consumed": [item["name"] for item in result["consumed"]],
                "rejected": [[item["name"], item["reason"]] for item in result["rejected"]]}

    def _human_wait(self, outcome: Outcome) -> Outcome | None:
        """Request-answerable needs-human with --human-wait left: poll the inbox, stop and the deadline (no turn).
        None means a decision was consumed and the loop continues."""
        until = self.clock() + self.bounds.human_wait_seconds
        while self.clock() < until and self._elapsed() < self.bounds.deadline_seconds:
            _sleep(max(0.0, min(self.bounds.poll_seconds, until - self.clock())))
            stop = self._stop()
            if stop is not None:
                return stop
            if self._consume()["consumed"]:
                return None
        return outcome

    def _judge(self, step) -> tuple[dict, bool] | Outcome:
        """One judgment key: reserve it (fresh) and launch it exactly once through the isolated entry. A refusal before
        the reservation reserves nothing: cited artifacts that differ from the contributor's observed return are
        needs-human/stale-input, anything else (for example an invalid root) needs-human/judgment-preflight."""
        action, role = step.action, self.pipeline["roles"]["judgment"]
        subjects = {"step_id": step.step_id, "request_id": action["request_id"], "judgment_key": action["key"]}
        if self._elapsed() + role["timeout"] > self.bounds.deadline_seconds:
            return self._stop() or Outcome("continue", "deadline", subjects)
        stopped = self._cost_stop(step, "judgment")
        if stopped is not None:
            return stopped
        if action["fresh"]:
            try:   # launch-boundary L7: the boundary checked before the key is reserved
                clients.boundary_ready(role.get("boundary"), clients.get(role["client"]), self.repo)
                if role.get("mode") == "read":   # Copilot reviewer design V7, V8: the version, before the key
                    clients.read_ready(clients.get(role["client"]), clients.preflight(clients.get(role["client"]))[1])
                judgment.prepare(self.repo, self.store, self.owner, self.feature_id, action["request_id"],
                                 self.lease["pipeline_sha256"], step.step_id, action["key"],
                                 self.lease["judgment_root"], hook=_hook)
            except DomainError as exc:
                if exc.code == "OWNERSHIP_CONFLICT" or \
                   action["key"] in judgment.reservations(self.store.read_export_snapshot(self.owner)):
                    raise
                refused = {key: value for key, value in subjects.items() if key != "judgment_key"}
                return Outcome("needs-human", "stale-input" if exc.code == "STALE_INPUT" else "judgment-preflight",
                               {**refused, "next_judgment_key": action["key"], "gate": exc.code,
                                "detail": exc.message})
        self.judging = action["key"]
        result = judgment.run(self.repo, self.store, self.owner, action["key"], role, self.lease["judgment_root"],
                              hook=_hook)
        self.judging = None
        launched = result != "failed-preflight"
        if launched:
            self.launches["judgment"] = self.launches.get("judgment", 0) + 1
        state = result["state"] if isinstance(result, dict) else result
        return {"kind": "judge", **subjects, "client": role["client"], "state": state,
                **({"verdict": result["verdict"], "valid": result["valid"]} if isinstance(result, dict) else {})}, \
            launched

    def _act(self, step) -> tuple[dict, bool] | Outcome:
        action = step.action
        coordinator = Coordinator(self.store)
        if action["kind"] == "judge":
            return self._judge(step)
        if action["kind"] == "decide":
            decided = judgment.record(self.repo, self.store, self.owner, self.feature_id, action["request_id"],
                                      action["key"])
            self.decisions.append(decided)
            return {"kind": "decide", "step_id": step.step_id, "request_id": action["request_id"],
                    "judgment_key": action["key"], "decision_id": decided["id"]}, False
        if action["kind"] == "cancel-stale":
            coordinator.cancel_prepared(self.owner, action["assignment_id"])
            return {"kind": "cancel-stale", "step_id": step.step_id, "assignment_id": action["assignment_id"]}, False
        if action["kind"] in ("prepare", "review-prepare"):
            planned = action["kind"] == "prepare"
            story = planned and action.get("profile") in implementation.STORY_PROFILES
            cls = implementation.ImplementationPrepare if story else \
                bmad_planning.PlanningPrepare if planned else bmad_review.ReviewPrepare
            spec = cls(**action["spec"])
            author = story and action["profile"] == implementation.SPEC_AUTHOR
            try:
                prepared = (bmad_spec_author.prepare if author else implementation.prepare if story else
                            bmad_planning.prepare if planned else
                            bmad_review.review_prepare)(self.repo, self.store, self.owner, spec)
            except DomainError as exc:
                if exc.code != "IDEMPOTENCY_CONFLICT" or not action.get("finishes"):
                    raise
                coordinator.cancel_prepared(self.owner, action["finishes"])  # half-finished prepare differs: re-key
                return {"kind": "cancel-stale", "step_id": step.step_id, "assignment_id": action["finishes"],
                        "key": spec.key}, False
            if planned and (spec.corrects_review or getattr(spec, "corrects_verify", None)):
                self.corrections += 1
            return {"kind": action["kind"], "step_id": step.step_id, "key": spec.key,
                    "assignment_id": prepared["assignment_id"],
                    **({"intent": spec.intent} if planned else {}),
                    **({"corrects_review": spec.corrects_review} if planned and spec.corrects_review else {})}, False
        # The story actions (implementation-drive design D9-D11, D18, ruling R6): thin calls into the module that holds
        # the gates. A sealed story receipt or status commit is progress (its `story.*` anchor is a driver event).
        if action["kind"] == "setup":
            feature, project = self._feature(), self.pipeline["project"]
            found = implementation.setup_worktree(self.repo, self.store, self.owner, feature, project,
                                                  action.get("start"))
            failed = [item["name"] for record in (found["setup"], found["baseline"]) if record
                      for item in record["results"] if item["outcome"] not in ("pass", "skipped")]
            failed += [found["setup"]["reason"]] if found["setup"].get("reason") else []   # the Git preflight
            if failed or found["baseline"] is None or found["baseline"]["outcome"] != "pass":
                baseline = found["baseline"] and found["baseline"]["outcome"]
                return Outcome("needs-human", "verify-env", {"step_id": step.step_id, "failed": failed,
                                                             "setup": found["setup"]["outcome"], "baseline": baseline})
            return {"kind": "setup", "step_id": step.step_id, "setup": found["setup"]["outcome"],
                    "baseline": found["baseline"]["outcome"]}, True
        if action["kind"] == "commit":
            try:
                record = implementation.commit_attempt(self.repo, self.store, self.owner, self._feature(),
                                                       action["assignment_id"], self.pipeline["project"], hook=_hook)
            except DomainError as exc:
                if not _drifted(exc):
                    raise
                expected = implementation.read_assignment_binding(self.store, self.repo,
                                                                  action["assignment_id"])["parent_sha"]
                return _drift(step, exc, expected, action["assignment_id"])
            self.commits += 1
            return {"kind": "commit", "step_id": step.step_id, "assignment_id": action["assignment_id"],
                    "commit_sha": record["sha"], "empty": record["empty"]}, True
        if action["kind"] == "verify":
            facts = implementation.story_facts(self.base, self.store.read_export_snapshot(self.owner), self.feature_id)
            commit = facts["commits"][action["assignment_id"]]
            try:   # the module compares HEAD with the recorded commit
                record = implementation.verify_attempt(self.repo, self.store, self.owner, self._feature(),
                                                       action["assignment_id"], commit, self.pipeline["project"])
            except DomainError as exc:
                if not _drifted(exc):
                    raise
                return _drift(step, exc, commit.get("sha"), action["assignment_id"])
            self.verifications += 1
            return {"kind": "verify", "step_id": step.step_id, "assignment_id": action["assignment_id"],
                    "outcome": record["outcome"], "commit_sha": record["commit_sha"]}, True
        if action["kind"] == "status":
            feature = self._feature()
            if action["phase"] == "spec":   # design D24 (ruling R3): the receipt names the accepted spec commit
                result, entries = {key: action[key] for key in ("assignment_id", "commit_sha", "bundle_id", "review_id",
                                                                "acceptance_id")}, []
            else:
                result, entries = (None, []) if action["phase"] == "start" else self._story_result(action)
            # the start commit sits on the base commit (setup ran there; an epic story's spec status commit, design
            # D24), the spec and done commits on the accepted commit
            expected = action.get("start", feature["base_commit"]) if action["phase"] == "start" else \
                result["commit_sha"]
            try:
                record = implementation.sync_status(self.repo, self.store, self.owner, feature, action["phase"],
                                                    self.pipeline["project"], result, entries, hook=_hook,
                                                    expected_parent=expected)
            except DomainError as exc:
                if exc.code == "STALE_INPUT" and exc.message.startswith("status-shape"):
                    return Outcome("needs-human", "status-shape", {"step_id": step.step_id, "detail": exc.message})
                if exc.code == "BLOCKED_EVIDENCE":   # a refused re-adoption: never reported as a stale acceptance
                    return Outcome("needs-human", "stale-input", {"step_id": step.step_id, "gate": exc.code,
                                                                  "detail": exc.message})
                if not _drifted(exc):
                    raise
                return _drift(step, exc, expected)
            return {"kind": "status", "step_id": step.step_id, "phase": action["phase"], "status_sha": record["sha"],
                    "entries": len(entries)}, True
        if action["kind"] == "carry":
            lease = drive_pipeline.carry(self.repo, self.store, self.owner, self.lease, self.pipeline, self._feature(),
                                         action["status_sha"], hook=_hook)
            decision = next(row for row in self.store.read_export_snapshot(self.owner)["decisions"]
                            if row["feature_id"] == self.feature_id and
                            row["source_ref"] == drive_pipeline.SPEC_READY + action["status_sha"])
            self.decisions.append({"id": decision["id"], "source_kind": "coordinator",
                                   "source_ref": decision["source_ref"], "request_id": None})
            previous = self.lease["pipeline_sha256"]
            self._carried(lease)
            return {"kind": "carry", "step_id": step.step_id, "status_sha": action["status_sha"],
                    "decision_id": decision["id"], "mandate_revision": self.pipeline["mandate_revision"],
                    "pipeline_sha256": lease["pipeline_sha256"], "carried_from": previous}, True
        if action["kind"] == "decide-form":
            decided = coordinator.record_decision(self.owner, DecisionInput(
                self.feature_id, self.pipeline["mandate_revision"], "coordinator", "driver:form",
                "the request held more than one question; the driver re-asks one question per return (no judgment)",
                {"form_refusal": True}, action["request_id"]))
            self.decisions.append({"id": decided, "source_kind": "coordinator", "source_ref": "driver:form",
                                   "request_id": action["request_id"]})
            return {"kind": "decide-form", "step_id": step.step_id, "request_id": action["request_id"],
                    "decision_id": decided}, False
        if action["kind"] == "bundle" and action.get("commit"):
            try:   # the module compares HEAD with the recorded commit
                bundle = implementation.bundle_for_commit(self.repo, self.store, self.owner, self.feature_id,
                                                          action["assignment_id"])
            except DomainError as exc:
                if not _drifted(exc):
                    raise
                return _drift(step, exc, step.subjects.get("commit_sha"), action["assignment_id"])
            return {"kind": "bundle", "step_id": step.step_id, "bundle_id": bundle["id"],
                    "artifact_paths": bundle["artifact_paths"][:20], "commit_sha": bundle["commit_sha"],
                    "source_assignment_ids": bundle["source_assignment_ids"]}, False
        # The slice-3 wrappers: thin calls into the same gates the attended `bmad` commands use.
        if action["kind"] == "bundle":
            bundle = bmad_review.bundle_for_workspace(self.repo, self.store, self.owner, bmad_review.BundleRequest(
                self.feature_id, action["workspace"]))
            return {"kind": "bundle", "step_id": step.step_id, "bundle_id": bundle["id"],
                    "artifact_paths": bundle["artifact_paths"][:20],
                    "source_assignment_ids": bundle["source_assignment_ids"]}, False
        if action["kind"] == "review-record":
            review = bmad_review.record_bound_review(self.repo, self.store, self.owner, action["assignment_id"])
            return {"kind": "review-record", "step_id": step.step_id, "assignment_id": action["assignment_id"],
                    "review_id": review["id"], "critical_open": review["critical_open"]}, False
        if action["kind"] == "accept":
            accepted = bmad_review.accept_bound(self.store, self.owner, action["bundle_id"], action["review_id"])
            return {"kind": "accept", "step_id": step.step_id, "bundle_id": action["bundle_id"],
                    "review_id": action["review_id"], "acceptance_id": accepted["id"]}, False
        if action["kind"] == "export":
            try:
                receipt = exporter.export_checkpoint(self.store, self.owner, Path(action["to"]))
            except DomainError as exc:  # a handoff target problem needs a human, never failed/internal-error
                if exc.code in ("OWNERSHIP_CONFLICT", "CORRUPT_STATE", "INCOMPATIBLE_SCHEMA"):
                    raise
                return Outcome("needs-human", "stale-input", {"step_id": step.step_id, "to": action["to"],
                                                              "gate": exc.code, "detail": exc.message})
            except OSError as exc:
                return Outcome("needs-human", "stale-input", {"step_id": step.step_id, "to": action["to"],
                                                              "gate": "OS_ERROR", "detail": str(exc)[:300]})
            return {"kind": "export", "step_id": step.step_id, "to": action["to"], "revision": receipt.revision,
                    "directory": receipt.directory.name, "digest": receipt.digest}, True
        role = action["role"]
        timeout = self.pipeline["roles"][role]["timeout"]
        if self._elapsed() + timeout > self.bounds.deadline_seconds:
            return self._stop() or Outcome("continue", "deadline", {"step_id": step.step_id,
                                                                    "assignment_id": action["assignment_id"]})
        stopped = self._cost_stop(step, role)
        if stopped is not None:
            return stopped
        self.attempt = action["assignment_id"]
        report = Launcher(self.repo, self.store, self.owner, self.pipeline).launch(action["assignment_id"], role)
        self.attempt = None
        self.launches[role] = self.launches.get(role, 0) + 1
        return {"kind": "launch", "step_id": step.step_id, "assignment_id": action["assignment_id"], "role": role,
                "client": self.pipeline["roles"][role]["client"], "state": report.state,
                "evidence_needs": report.evidence_needs[:5]}, True

    def _cost_stop(self, step, role: str) -> "Outcome | None":
        """Bill design B5/B6: before a launch, the story's reported USD (every lease, revision and chain of the
        feature) against the pipeline's `budgets.max_cost_usd`, then the epic's against its own bound; at or above a
        bound the step stops `cost-budget`. Reported money only: the subjects carry the launches without a figure."""
        scopes = []
        bound = self.pipeline["budgets"].get("max_cost_usd") or 0
        if bound:
            scopes.append(("story", bound, [self.feature_id]))
        if self.epic_bound and self.epic_bound[0]:
            scopes.append(("epic", self.epic_bound[0], list(self.epic_bound[1])))
        if not scopes:
            return None
        snapshot = self.store.read_snapshot()
        for scope, limit, features in scopes:
            used, unreported = bill.usd_reported(self.base, snapshot, features)
            if used >= limit:
                return Outcome("needs-human", "cost-budget", {"step_id": step.step_id, "role": role, "scope": scope,
                                                              "used": used, "max": limit,
                                                              "unreported_launches": unreported})
        return None

    def _bill(self) -> dict:
        """Bill design B4: the run's and the lease's launches so far, observed apart from reported."""
        snapshot = self.store.read_snapshot()
        return {"run": bill.summary(self.base, snapshot, [self.feature_id], self.lease_id, self.run_id),
                "lease": bill.summary(self.base, snapshot, [self.feature_id], self.lease_id)}

    def _carried(self, lease: dict) -> None:
        """Design D24: the run continues on the carried pipeline under the re-sealed lease."""
        self.lease, self.pipeline = lease, drive_pipeline.load(self.base, lease["pipeline_sha256"])
        self.carries += 1

    def _feature(self) -> dict:
        return next(row for row in self.store.read_export_snapshot(self.owner)["features"]
                    if row["id"] == self.feature_id)

    def _story_result(self, action) -> tuple[dict, list]:
        """Design D18/D19: the spec result section and the deferred-work entries from stored rows only. The result
        carries ids, the commit sha, verify names with their closed outcome and the judgment decisions' ids with their
        source kind, never free text; free text goes to the deferred-work entries, which the editor writes on one line
        each."""
        snapshot = self.store.read_export_snapshot(self.owner)
        feature = next(row for row in snapshot["features"] if row["id"] == self.feature_id)
        story = json.loads(feature["constraints_json"])
        spec_name = PurePosixPath(story["spec_path"]).name
        facts = implementation.story_facts(self.base, snapshot, self.feature_id)
        bundle = next(row for row in snapshot["bundles"] if row["id"] == action["bundle_id"])
        manifest = bundles.read_manifest(Path(bundle["path"]), bundle["manifest_digest"])
        head = manifest["commit"]["assignment_id"]
        verify = facts["verifies"].get(head) or {}
        decisions = [row for row in snapshot["decisions"] if row["feature_id"] == self.feature_id]
        listed, entries = [], []
        for row in decisions:
            choice = json.loads(row["choice_json"])
            judged = choice.get("judgment") if isinstance(choice, dict) else None
            if not isinstance(judged, dict):   # D18 lists judgment decisions only (not compat, form or inbox ones)
                continue
            deferred = choice.get("deferred") if isinstance(choice.get("deferred"), list) else None
            listed.append(f"{row['id']} ({row['source_kind']}{', deferred' if deferred else ''})")
            if deferred:   # dated by the judgment's sealed return, so a re-adoption seals the same entries
                day = drive_pipeline.read_sealed_json(self.base, judged["receipt_ref"],
                                                      judged["receipt_sha256"])["classified_at"][:10]
            for item in deferred or []:
                entries.append({"title": item["title"], "origin": f"coordinator {row['id']}",
                                "location": story["spec_path"], "source_spec": spec_name, "severity": "medium",
                                "reason": f"{item['context']} Revisit when: {item['revisit_when']}",
                                "decision": f"{day} {item['choice']}"})
        review = next(row for row in snapshot["reviews"] if row["id"] == action["review_id"])
        accepted = "accepted under accept_on_high: " if self.pipeline.get("accept_on_high") else \
            "accepted attended after an open-high-findings stop: "
        for pointer in open_high_findings(review["finding_json"]):
            view = json.loads(review["finding_json"])["finding"]
            name, index = pointer.strip("/").split("/")
            item = view[name][int(index)]
            entries.append({"title": f"Open high finding {pointer} of review {review['id'][:8]}",
                            "origin": f"coordinator {review['id']}",
                            "location": item.get("location", story["spec_path"]), "source_spec": spec_name,
                            "severity": "high",
                            "reason": accepted + json.dumps(item.get("potential_consequence", item)),
                            "decision": None})
        result = {"acceptance_id": action["acceptance_id"], "commit_sha": manifest["commit"]["sha"],
                  "review_id": action["review_id"],
                  "verify": [f"{item['name']}: {item['outcome']}" for item in verify.get("results", [])],
                  "decisions": listed,
                  "bill": bill.result_line(bill.summary(self.base, snapshot, [self.feature_id]))}   # B4
        return result, entries

    def _failure(self, exc: DomainError) -> Outcome:
        if exc.code == "OWNERSHIP_CONFLICT":
            return Outcome("failed", "ownership-lost")
        if exc.code in ("CORRUPT_STATE", "INCOMPATIBLE_SCHEMA"):
            return Outcome("failed", "corrupt-state", {"detail": exc.message})
        if exc.code in ("STALE_INPUT", "BLOCKED_EVIDENCE", "UNKNOWN_EVIDENCE", "INVALID_REPOSITORY",
                        "UNSUPPORTED_CONTRACT", "UNSUPPORTED_CLIENT_MODE", "UNSAFE_PATH", "CHECKPOINT_CONFLICT",
                        "INVALID_CHECKPOINT"):
            return Outcome("needs-human", GATE_REASONS.get(exc.code, "stale-input"),
                           {"gate": exc.code, "detail": exc.message})
        return Outcome("failed", "internal-error", {"gate": exc.code, "detail": exc.message})

    def _end(self, outcome: Outcome) -> tuple[dict, int]:
        self.ending = True
        if outcome.outcome == "continue" and self.graceful:  # N1: a SIGINT that arrived while ending
            outcome = Outcome("needs-human", "signal-stop", outcome.subjects)
        subjects = outcome.subjects or {}
        counts = {"turns": self.turns, "launches": dict(self.launches), "decisions": {},
                  "corrections": self.corrections, "commits": self.commits, "verifications": self.verifications,
                  "carries": self.carries}
        for item in self.decisions:
            counts["decisions"][item["source_kind"]] = counts["decisions"].get(item["source_kind"], 0) + 1
        stop = outcome.reason in STOP_REASONS
        summary = {"kind": "drive-summary", "format": 1, "lease_id": self.lease_id, "run_id": self.run_id,
                   "feature_id": self.feature_id, "generation": self.owner.generation,
                   "pipeline_sha256": self.lease["pipeline_sha256"], "outcome": outcome.outcome,
                   "reason": outcome.reason, "subjects": subjects,
                   "operator_action": _operator_action(outcome.outcome, outcome.reason, subjects),
                   "ownership": "unchanged" if outcome.outcome == "failed" else "retained", "ownership_reason": None,
                   "decisions": self._listed_decisions(outcome), "counts": counts, "bill": self._bill(),
                   "steps": [step.view() for step in self.steps],
                   "restarts": self.restarts, "interventions": self.interventions,
                   "acknowledged_stops": self.acknowledged if stop else [], "ended_at": _now()}
        if outcome.reason == "ownership-lost" or self.run_id is None:
            return summary, EXIT[outcome.outcome]   # no further database write once ownership is lost
        release = False
        try:
            if outcome.outcome == "done":
                blocked = _releasable(self.store.read_export_snapshot(self.owner))
                if not self.release_on_done:   # design epic-drive E7: the epic, not the story, releases the session
                    summary["ownership"] = "retained"
                    summary["ownership_reason"] = "epic run" if blocked is None else f"epic run; {blocked}"
                else:
                    summary["ownership"], summary["ownership_reason"] = ("retained", blocked) if blocked else \
                        ("released", None)
                release = blocked is None and self.release_on_done
            self._anchor("drive.stop-acknowledged" if stop else "drive.ended", self.dir / "summary.json", summary)
            _hook("after-ended", run_id=self.run_id)
            if release:
                Coordinator(self.store).release(self.owner)
        except (DomainError, OSError) as exc:
            error = {"code": getattr(exc, "code", "OS_ERROR"), "message": getattr(exc, "message", str(exc))}
            summary = {**summary, "end_error": error}
            if release:  # the sealed summary recorded the release decided under LOCK_EX; owner.released is the fact
                summary.update(ownership="retained", ownership_reason=f"not released: {error['code']}")
        return summary, EXIT[outcome.outcome]

    def _listed_decisions(self, outcome: Outcome) -> list[dict]:
        """This run's decisions, then every judgment-backed coordinator decision of the feature that an earlier run
        recorded (`earlier_run`), so each one is listed for review even after a kill before `drive.ended`. Counts stay
        this run's."""
        if outcome.reason == "ownership-lost" or self.run_id is None:
            return list(self.decisions)
        try:
            every = {item["id"]: item for item in judgment.decisions(self.store.read_export_snapshot(self.owner),
                                                                     self.feature_id)}
        except DomainError:
            return list(self.decisions)
        mine = [{**item, **every.get(item["id"], {})} for item in self.decisions]
        listed = {item["id"] for item in mine}
        return mine + [{**item, "source_kind": "coordinator", "earlier_run": True} for item in every.values()
                       if item["id"] not in listed]


# Status -------------------------------------------------------------------------------------------------------------

def status(repo, feature_id: str | None = None) -> dict:
    """Read-only; creates nothing: the lock from /proc/locks, leases and their last run, derived steps of each
    bound pipeline, the inbox, open requests and attempt windows."""
    try:
        store = open_store(repo)
    except DomainError as exc:
        if exc.code == "UNINITIALIZED":
            return {"state": "uninitialized"}
        raise
    base = store.path.parent
    snapshot = store.read_snapshot()
    events = snapshot["events"]
    leases, newest = [], {}
    for row in events:   # a carry re-seals its lease (design D24): the newest record of each lease decides
        if row["kind"] == "drive.leased" and not (feature_id and row["source_id"] != feature_id):
            newest[row["subject_id"]] = row
    for row in newest.values():
        runs = [item["source_id"] for item in events if item["kind"] == "drive.started" and
                item["subject_id"] == row["subject_id"]]
        ends = {item["source_id"]: item for item in events if item["subject_id"] == row["subject_id"] and
                item["kind"] in ("drive.ended", "drive.stop-acknowledged")}
        last = None
        if runs:
            summary = _sealed(base, ends[runs[-1]]) if runs[-1] in ends else None
            last = {"run_id": runs[-1], "ended": runs[-1] in ends,
                    "end_event": ends[runs[-1]]["kind"] if runs[-1] in ends else None,
                    "outcome": summary and summary.get("outcome"), "reason": summary and summary.get("reason"),
                    "interventions": len(summary.get("interventions") or []) if summary else None,
                    "corrections": (summary.get("counts") or {}).get("corrections") if summary else None}
        lease = _sealed(base, row) or {}
        leases.append({"lease_id": row["subject_id"], "feature_id": row["source_id"],
                       "pipeline_sha256": lease.get("pipeline_sha256"), "generation": lease.get("generation"),
                       "runs": len(runs), "last_run": last})
    features, windows = [], []
    for feature in snapshot["features"]:
        if feature_id and feature["id"] != feature_id:
            continue
        try:
            bound = drive_pipeline.active(base, snapshot, feature["id"])
        except DomainError as exc:   # a sealed pipeline the rules now refuse (a story pipeline from before D22)
            features.append({"feature_id": feature["id"], "pipeline_sha256": None, "steps": [],
                             "error": {"code": exc.code, "message": exc.message}})
            continue
        if bound is None:
            continue
        facts = drive_pipeline.gather(repo, store, snapshot, bound["value"], snapshot["owner"]["generation"])
        steps = drive_pipeline.derive(bound["value"], bound["sha256"], snapshot, facts)
        features.append({"feature_id": feature["id"], "pipeline_sha256": bound["sha256"],
                         "steps": [step.view() for step in steps]})
        windows += inbox.windows(repo, snapshot, feature["id"])
    return {"repository_id": snapshot["repository_id"], "owner": snapshot["owner"], "lock": lock_state(repo),
            "leases": leases, "features": features, "inbox": inbox.listing(repo, store, feature_id),
            "open_requests": [row["id"] for row in snapshot["decision_requests"] if row["state"] == "open" and
                              (feature_id is None or row["feature_id"] == feature_id)],
            "windows": windows}
