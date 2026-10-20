"""Operator inbox (unattended-driver design D10 and section 6): immutable private files under
<common-dir>/coordinator/inbox/, written without a token and never by the database writer; only the owner consumes them,
atomically with their effect, at turn boundaries. A decision linked inside an attempt window is rejected and can be
re-sent once the window closes; stop messages are exempt. Same-user trust is an accepted, documented limit."""

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import judgment
from .drive_pipeline import read_sealed_json
from .model import DecisionInput, DomainError, OwnerRef
from .run_receipt import canonical, private_file, read_receipt, run_name, run_root, seal
from .service import TERMINAL_STATES, Coordinator, holds_fence
from .store import open_store
from .worker import _launch_intent

KIND, FORMAT = "coordinator-inbox", 1
NAME = re.compile(r"^([0-9]{1,24})-([0-9a-f]{16})\.json$")
COMMON = ("kind", "format", "type", "repository_id", "feature_id", "generation", "reason", "source_ref")
FIELDS = {"stop": COMMON, "decision": COMMON + ("request_id", "mandate_revision", "choice")}
MAX_BYTES = 1 << 20
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_INPUT", message)


def _coordinator_dir(repo) -> Path:
    directory = repo.common_dir / "coordinator"
    if directory.is_symlink():
        raise DomainError("UNSAFE_PATH", "Coordinator state path is a symlink")
    return directory


def publish(repo, value: dict) -> dict:
    """Canonical JSON to a private temp file, fsync, exclusive link to <created_ns>-<sha256[:16]>.json, directory
    fsync (run_receipt.private_file). Never creates the coordinator directory; never edits or deletes a message."""
    directory = _coordinator_dir(repo)
    if not directory.is_dir():
        raise DomainError("UNINITIALIZED", "Coordinator has not been initialized")
    box = directory / "inbox"
    if box.is_symlink():
        raise DomainError("UNSAFE_PATH", "Inbox directory is a symlink")
    box.mkdir(mode=0o700, exist_ok=True)
    if box.is_symlink() or not box.is_dir():
        raise DomainError("UNSAFE_PATH", "Inbox directory is unsafe")
    data = canonical(value)
    digest = hashlib.sha256(data).hexdigest()
    name = f"{time.time_ns()}-{digest[:16]}.json"
    try:
        private_file(box / name, data)
    except FileExistsError as exc:
        raise _invalid("Inbox message name already exists") from exc
    return {"name": name, "sha256": digest, "type": value.get("type")}


def add(repo, kind: str, data: dict) -> dict:
    """`inbox add decision|stop`: refuses UNINITIALIZED without a store; generation from a read-only snapshot."""
    if kind not in FIELDS:
        raise _invalid("Inbox message type must be decision or stop")
    required = ("feature_id", "reason", "source_ref") + (("request_id", "choice") if kind == "decision" else ())
    optional = ("mandate_revision",) if kind == "decision" else ()
    if not isinstance(data, dict) or set(required) - set(data) or set(data) - set(required) - set(optional):
        raise _invalid("Inbox message fields do not match the command schema")
    for key in ("feature_id", "reason", "source_ref") + (("request_id",) if kind == "decision" else ()):
        if type(data[key]) is not str or not data[key].strip():
            raise _invalid(f"Inbox message {key} must be non-empty text")
    snapshot = open_store(repo).read_snapshot()
    feature = next((row for row in snapshot["features"] if row["id"] == data["feature_id"]), None)
    if feature is None:
        raise DomainError("NOT_FOUND", "Feature does not exist")
    value = {"kind": KIND, "format": FORMAT, "type": kind, "repository_id": snapshot["repository_id"],
             "feature_id": data["feature_id"], "generation": snapshot["owner"]["generation"],
             "reason": data["reason"], "source_ref": data["source_ref"]}
    if kind == "decision":
        revision = data.get("mandate_revision", feature["mandate_revision"])
        if type(revision) is not int:
            raise _invalid("Inbox decision mandate revision must be an integer")
        value.update(request_id=data["request_id"], mandate_revision=revision, choice=data["choice"])
    canonical(value)
    return publish(repo, value)


@dataclass(frozen=True)
class Message:
    name: str
    sha256: str | None
    value: dict | None
    defect: str | None            # nonprivate | tampered | malformed
    ctime_ns: int


def _order(name: str):
    match = NAME.match(name)
    return (0, int(match.group(1)), name) if match else (1, 0, name)


def messages(base: Path) -> list[Message]:
    box = base / "inbox"
    if box.is_symlink() or not box.is_dir():
        return []
    found = []
    for name in sorted((item for item in os.listdir(box) if not item.startswith(".")), key=_order):
        path = box / name
        try:
            info = path.lstat()
        except OSError:
            continue
        match, value, digest, defect = NAME.match(name), None, None, None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or \
           info.st_nlink != 1:
            defect = "nonprivate"
        elif not match or info.st_size > MAX_BYTES:
            defect = "malformed"
        else:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
                digest = hashlib.sha256(data).hexdigest()
                if digest[:16] != match.group(2):
                    defect = "tampered"
                else:
                    value = json.loads(data)
                    if not isinstance(value, dict) or canonical(value) != data or value.get("kind") != KIND or \
                       value.get("format") != FORMAT or value.get("type") not in FIELDS or \
                       set(value) != set(FIELDS[value["type"]]) or type(value.get("generation")) is not int:
                        value, defect = None, "malformed"
            except (OSError, ValueError, UnicodeError):
                value, defect = None, "malformed"
        found.append(Message(name, digest, value, defect, info.st_ctime_ns))
    return found


def _sealed_record(base: Path, ref: str, digest: str) -> dict | None:
    try:
        return read_sealed_json(base, ref, digest)
    except (DomainError, ValueError):
        return None


def ledger(base: Path, snapshot: dict) -> dict:
    """What happened to each message, derived from rows and anchors only: consumed decisions (by source_ref),
    rejections (inbox.rejected) and acknowledged stops (named by a drive.stop-acknowledged summary)."""
    consumed = {}
    for row in snapshot["decisions"]:
        if row["source_kind"] == "operator-reported" and row["source_ref"].startswith("inbox/"):
            name, _, digest = row["source_ref"][len("inbox/"):].partition("#")
            consumed[name] = {"decision_id": row["id"], "sha256": digest}
    rejected = {row["subject_id"]: {"reason": row["source_id"], "ref": row["evidence_ref"],
                                    "digest": row["payload_digest"]}
                for row in snapshot["events"] if row["kind"] == "inbox.rejected"}
    acknowledged = {}
    for row in snapshot["events"]:
        if row["kind"] == "drive.stop-acknowledged":
            summary = _sealed_record(base, row["evidence_ref"], row["payload_digest"]) or {}
            for name in summary.get("acknowledged_stops", []):
                acknowledged.setdefault(name, row["source_id"])
    return {"consumed": consumed, "rejected": rejected, "acknowledged": acknowledged}


def _ns(text) -> int | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return (moment - EPOCH) // timedelta(microseconds=1) * 1000


def _anchored_time(repo, ref, digest, key: str) -> int | None:
    """A timestamp from a digest-anchored run receipt (launch.json or return.json), or None."""
    try:
        return _ns(read_receipt(repo, ref, digest).get(key)) if ref and digest else None
    except DomainError:
        return None


def windows(repo, snapshot: dict, feature_id: str) -> list[dict]:
    """Attempt and judgment windows of the driven feature (judgment windows: judgment.windows).

    Start: the `observed_at` of the launch receipt anchored by `assignment.launch-observed`, or the launch intent's
    `observed_at` when that is earlier (an earlier start only widens the window); with neither, the run directory's
    ctime. End: the anchored `ended_at` of the terminal classification, read from the digest-anchored return receipt,
    or the `resolved_at` of the sealed resolution record. A fence holder's window is open (end None), and so is one
    whose anchored end cannot be read. Remaining approximation: both ends are wall-clock stamps written just before
    the database classification (after a kill, reconcile classifies later), compared with the filesystem ctime."""
    base = repo.common_dir / "coordinator"
    events = snapshot["events"]
    launches = {row["subject_id"]: row for row in events if row["kind"] == "assignment.launch-observed"}
    resolutions = {row["subject_id"]: row for row in events if row["kind"] == "assignment.resolved-unknown"}
    found = []
    for row in snapshot["assignments"]:
        if row["feature_id"] != feature_id or row["state"] in ("prepared", "cancelled-before-launch"):
            continue
        directory = run_root(repo) / run_name(row["id"], row["request_fingerprint"])
        if not os.path.lexists(directory):
            continue
        launch = launches.get(row["id"])
        intent = _launch_intent(directory)
        starts = [_anchored_time(repo, launch["evidence_ref"], launch["payload_digest"], "observed_at")
                  if launch else None, _ns(intent.get("observed_at")) if intent else None]
        starts = [item for item in starts if item is not None]
        start = min(starts) if starts else directory.lstat().st_ctime_ns
        end = None
        if row["state"] == "resolved-unknown" and row["id"] in resolutions:
            event = resolutions[row["id"]]
            end = _ns((_sealed_record(base, event["evidence_ref"], event["payload_digest"]) or {}).get("resolved_at"))
        elif row["state"] in TERMINAL_STATES and not holds_fence(row["state"], row["return_json"]):
            ret = json.loads(row["return_json"])
            end = _anchored_time(repo, ret.get("event_ref"), ret.get("payload_digest"), "ended_at")
        elif not holds_fence(row["state"], row["return_json"]):
            end = start
        found.append({"subject": f"assignment:{row['id']}", "start_ns": start, "end_ns": end, "open": end is None})
    found += judgment.windows(base, snapshot, feature_id)
    return sorted(found, key=lambda item: item["start_ns"])


def _window_of(ctime_ns: int, found: list[dict]) -> dict | None:
    return next((item for item in found if item["start_ns"] <= ctime_ns and
                 (item["end_ns"] is None or ctime_ns <= item["end_ns"])), None)


def listing(repo, store, feature_id: str | None = None) -> list[dict]:
    """Read-only: every message as pending, consumed or rejected, with the reason and the window subject."""
    base = store.path.parent
    snapshot = store.read_snapshot()
    known = ledger(base, snapshot)
    result = []
    for message in messages(base):
        value = message.value or {}
        if feature_id is not None and value and value.get("feature_id") != feature_id:
            continue
        entry = {"name": message.name, "sha256": message.sha256, "type": value.get("type"),
                 "feature_id": value.get("feature_id"), "request_id": value.get("request_id"),
                 "generation": value.get("generation"), "state": "pending", "reason": None, "subject": None}
        if message.name in known["rejected"]:
            item = known["rejected"][message.name]
            record = _sealed_record(base, item["ref"], item["digest"]) or {}
            entry.update(state="rejected", reason=item["reason"], subject=record.get("subject"))
        elif message.name in known["consumed"]:
            entry.update(state="consumed", decision_id=known["consumed"][message.name]["decision_id"])
        elif message.name in known["acknowledged"]:
            entry.update(state="consumed", run_id=known["acknowledged"][message.name])
        result.append(entry)
    return result


def reject(store, owner: OwnerRef, record_dir: Path, message: Message, reason: str, subject: str | None) -> dict:
    """Seal the rejection record in the run directory and anchor `inbox.rejected` (subject: the message name,
    source: the reason). The message file itself is never touched."""
    base = store.path.parent
    record = {"kind": "inbox-rejection", "format": 1, "name": message.name, "reason": reason, "subject": subject,
              "message_sha256": message.sha256, "rejected_at": datetime.now(timezone.utc).isoformat()}
    path = Path(record_dir) / f"inbox-rejected-{hashlib.sha256(message.name.encode()).hexdigest()[:16]}.json"
    digest = seal(path, record)
    Coordinator(store).anchor_event(owner, "inbox.rejected", message.name,
                                    (reason, path.relative_to(base).as_posix(), digest))
    return {"name": message.name, "reason": reason, "subject": subject}


def _pending(base: Path, snapshot: dict) -> list[Message]:
    known = ledger(base, snapshot)
    return [message for message in messages(base) if message.name not in known["rejected"] and
            message.name not in known["consumed"] and message.name not in known["acknowledged"]]


def stops(repo, store, owner: OwnerRef, feature_id: str, generation: int, record_dir: Path) -> dict:
    """R3: valid stop messages of this feature and generation stay pending until drive.stop-acknowledged; a stop of
    another generation is rejected as stale, one from another repository as foreign. Exempt from windows."""
    base = store.path.parent
    snapshot = store.read_export_snapshot(owner)
    result = {"pending": [], "rejected": []}
    for message in _pending(base, snapshot):
        value = message.value
        if message.defect or value["type"] != "stop" or value["feature_id"] != feature_id:
            continue
        if value["repository_id"] != snapshot["repository_id"]:
            result["rejected"].append(reject(store, owner, record_dir, message, "foreign", None))
        elif value["generation"] != generation:
            result["rejected"].append(reject(store, owner, record_dir, message, "stale-generation", None))
        else:
            result["pending"].append(message.name)
    return result


def consume_decisions(repo, store, owner: OwnerRef, feature_id: str, record_dir: Path) -> dict:
    """R5 (or a human wait): each pending decision, in name order, is re-validated (private regular file of this
    uid, digest, repository, feature, ctime outside every window, open request, current revision) and is either
    recorded as an operator-reported decision whose source_ref names the file and digest (one transaction), or
    rejected once with a named reason. Defective messages of any type are rejected here."""
    base = store.path.parent
    snapshot = store.read_export_snapshot(owner)
    feature = next(row for row in snapshot["features"] if row["id"] == feature_id)
    found = windows(repo, snapshot, feature_id)
    requests = {row["id"]: row for row in snapshot["decision_requests"]}
    resolved, result = set(), {"consumed": [], "rejected": []}
    for message in _pending(base, snapshot):
        value = message.value
        reason, subject = message.defect, None
        if reason is None and value["repository_id"] != snapshot["repository_id"]:
            reason = "foreign"
        if reason is None and (value["type"] != "decision" or value["feature_id"] != feature_id):
            continue
        if reason is None:
            window = _window_of(message.ctime_ns, found)
            if window is not None:
                reason, subject = "written-during-attempt", window["subject"]
        if reason is None:
            request = requests.get(value["request_id"])
            if request is None or request["feature_id"] != feature_id or request["state"] != "open" or \
               value["request_id"] in resolved:
                reason = "request-not-open"
            elif value["mandate_revision"] != feature["mandate_revision"]:
                reason = "stale-revision"
        if reason is None:
            try:
                decision_id = Coordinator(store).record_decision(owner, DecisionInput(
                    feature_id, value["mandate_revision"], "operator-reported",
                    f"inbox/{message.name}#{message.sha256}", value["reason"], value["choice"], value["request_id"]))
            except DomainError as exc:
                if exc.code not in ("INVALID_INPUT", "REVISION_CONFLICT"):
                    raise
                reason = "request-not-open" if exc.code == "INVALID_INPUT" else "stale-revision"
            else:
                resolved.add(value["request_id"])
                result["consumed"].append({"name": message.name, "decision_id": decision_id,
                                           "request_id": value["request_id"]})
                continue
        result["rejected"].append(reject(store, owner, record_dir, message, reason, subject))
    return result
