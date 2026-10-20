"""Bundle wrapper, bound independent review and acceptance for installed BMAD planning output.

Thin layer over the core gates. `observed` review exists only through BoundReviewSource: a distinct worker process,
an anchored structured return and an exact sealed-bundle binding. What the reviewer read, understood or found stays
a model claim; nothing here parses command events as proof."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import bmad_contract as contract
from . import bundles, clients
from .bmad_implementation import SPEC_AUTHOR, STORY_PROFILES
from .bmad_planning import (AFFECTED_IDS_RULE, PLAN, REVIEW, _file_entry, _row, _write_brief, client_binding,
                            client_label, effective_dependencies, read_assignment_binding, refuse_while_running,
                            spec_digest)
from .model import AssignmentInput, BundleInput, DomainError, ReviewEvidence
from .profiles import ReturnProfile
from .repo import resolve_repo
from .run_receipt import canonical, read_binding, read_receipt, receipt_ref, run_name, seal_binding
from .service import Coordinator, _anchor_rows, _artifact_contributors, _path_identity, _resolved_worktree, \
    _safe_feature_paths, binding_anchor, resolving_decisions, sealed_decisions_state
from .worker import historical_return

SEVERITIES = ("critical", "high", "medium", "low")
KINDS = ("assumption", "partial")
DEFAULT_LENSES = ["adversarial", "edge-case-hunter"]
CODE_LENSES = ("acceptance", "matrix", "scope", "security", "regression")   # plan ruling R9: the code review lens set
SPEC_LENSES = ("readiness", "scope", "verification", "edges", "fidelity")   # design D24: the spec review lens set
LENS = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
FINDING_FIELDS = ("lens", "location", "trigger_condition", "guard_snippet", "potential_consequence", "severity")


@dataclass(frozen=True)
class BundleRequest:
    feature_id: str
    workspace: str
    dependency_paths: list[str] = field(default_factory=list)  # extra frozen dependencies; required ones are added


@dataclass(frozen=True)
class ReviewPrepare:
    bundle_id: str
    reviewer_id: str
    key: str
    compat_decision_id: str | None = None   # required for a planning bundle, refused for a code bundle (design D22)
    lenses: list[str] = field(default_factory=lambda: list(DEFAULT_LENSES))
    client_root: str = ".agents/skills"
    client: str = clients.DEFAULT                # registered write-free worker client


def _bad(message: str) -> DomainError:
    return DomainError("UNKNOWN_EVIDENCE", "Review return invalid: " + message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_object(*names, **special) -> dict:
    properties = {name: {"type": "string"} for name in names} | special
    return {"type": "object", "additionalProperties": False, "required": sorted(properties),
            "properties": properties}


def _exact(item, names, label: str) -> dict:
    if not isinstance(item, dict) or set(item) != set(names):
        raise _bad(f"{label} entry has the wrong fields")
    return item


def critical_open(payload: dict) -> bool:
    """Derived, never caller supplied: any critical severity among the findings or the dispositions."""
    return any(item["severity"] == "critical" for item in payload["findings"] + payload["dispositions"])


def finding_counts(finding_json) -> dict:
    """Design D26: a recorded review's findings and dispositions by severity (the basis of `critical_open`), every
    severity present; an unreadable record counts nothing."""
    counts = {name: 0 for name in SEVERITIES}
    try:
        view = json.loads(finding_json)["finding"]
        items = list(view.get("findings") or []) + list(view.get("dispositions") or [])
    except (KeyError, TypeError, ValueError, AttributeError):
        return counts
    for item in items:
        severity = item.get("severity") if isinstance(item, dict) else None
        if severity in counts:
            counts[severity] += 1
    return counts


def _sealed_files(bundle: dict, manifest: dict) -> list[dict]:
    """The reviewed subject: artifact and dependency files. Contract files are installed tooling bound by the
    fingerprint, not review subject."""
    return [{"kind": item["kind"], "path": item["path"], "digest": item["digest"],
             "sealed_path": str(Path(bundle["path"]) / item["kind"] / item["path"])}
            for item in manifest["files"] if item["kind"] in ("artifact", "dependency")]


def check(final, assignment, binding, run_dir, observed_changed) -> dict:
    """Profile check: a completed return must match the binding exactly; anything else is UNKNOWN_EVIDENCE."""
    if final["result"] != "completed":
        return {"valid": False, "reason": f"reviewer returned {final['result']}; that is never a review"}
    if observed_changed:
        raise _bad("a reviewer must not change files")
    if final["bundle_id"] != binding["bundle_id"] or final["manifest_digest"] != binding["manifest_digest"]:
        raise _bad("bundle ID or manifest digest differs from the binding")
    lenses = final["lenses_run"]
    if not isinstance(lenses, list) or any(type(item) is not str for item in lenses) or \
       len(set(lenses)) != len(lenses) or sorted(lenses) != sorted(binding["lenses"]):
        raise _bad("lenses run differ from the bound lens set")
    read = final["bundle_read"]
    if not isinstance(read, list) or any(type(_exact(item, ("path", "sha256"), "bundle_read")[key]) is not str
                                         for item in read for key in ("path", "sha256")):
        raise _bad("bundle_read is malformed")
    # F16 (live hook 19): a reviewer that names the sealed location it read instead of the manifest path attests the
    # same bytes; the sealed path is an alias of the manifest path, normalised before the comparison and in the record
    aliases = {item["sealed_path"]: item["path"] for item in binding["sealed_files"]}
    read = [{**item, "path": aliases.get(item["path"], item["path"])} for item in read]
    attested = sorted((item["path"], item["sha256"]) for item in read)
    if attested != sorted((item["path"], item["digest"]) for item in binding["sealed_files"]):
        raise _bad("per-file digest attestations do not equal the sealed bundle manifest")
    findings, dispositions = final["findings"], final["dispositions"]
    if not isinstance(findings, list) or not isinstance(dispositions, list):
        raise _bad("findings or dispositions are malformed")
    for item in findings:
        _exact(item, FINDING_FIELDS, "finding")
        if any(type(item[name]) is not str for name in FINDING_FIELDS) or item["severity"] not in SEVERITIES or \
           item["lens"] not in binding["lenses"]:
            raise _bad("finding has a non-text field, an unknown severity or an unbound lens")
    seen = []
    for item in dispositions:
        _exact(item, ("contributor_id", "kind", "index", "severity"), "disposition")
        if type(item["contributor_id"]) is not str or item["kind"] not in KINDS or type(item["index"]) is not int \
           or item["severity"] not in SEVERITIES:
            raise _bad("disposition has a malformed field")
        seen.append((item["contributor_id"], item["kind"], item["index"]))
    carried = binding["carried"]
    required = sorted((item["contributor_id"], item["kind"], item["index"])
                      for item in carried["assumptions"] + carried["critical_flags"])
    if sorted(seen) != required:
        raise _bad("dispositions must cover each carried contributor assumption and partial marker exactly once")
    payload = {"valid": True, "bundle_id": final["bundle_id"], "manifest_digest": final["manifest_digest"],
               "lenses_run": lenses, "bundle_read": sorted(read, key=lambda item: item["path"]),
               "findings": findings, "dispositions": sorted(dispositions, key=lambda item: (
                   item["contributor_id"], item["kind"], item["index"]))}
    payload["critical_open"] = critical_open(payload)
    return payload


def _review_view(payload: dict) -> dict:
    return {key: payload[key] for key in ("lenses_run", "bundle_read", "findings", "dispositions")}


def _bundle_row(snapshot: dict, binding: dict) -> tuple[dict, dict]:
    bundle = _row(snapshot["bundles"], binding["bundle_id"], "Bundle")
    manifest = bundles.read_manifest(Path(bundle["path"]), bundle["manifest_digest"])
    if bundle["manifest_digest"] != binding["manifest_digest"] or \
       binding["sealed_files"] != _sealed_files(bundle, manifest):
        raise DomainError("UNKNOWN_EVIDENCE", "Review binding no longer matches the bundle manifest")
    if not bundles.sealed_copy(Path(bundle["path"]), manifest):
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed bundle copy differs from its manifest")
    return bundle, manifest


def _bundle_spec(bundle: dict, manifest: dict) -> BundleInput:
    kinds = {kind: [item["path"] for item in manifest["files"] if item["kind"] == kind]
             for kind in ("artifact", "dependency")}
    return BundleInput(bundle["feature_id"], kinds["artifact"], kinds["dependency"],
                       list(manifest["source_assignment_ids"]),
                       [{"path": item["path"], "digest": item["digest"]}
                        for item in manifest["files"] if item["kind"] == "contract"], manifest.get("commit"))


def _contributors(snapshot: dict, bundle: dict) -> list[dict]:
    by_id = {row["id"]: row for row in snapshot["assignments"]}
    ids = json.loads(bundle["source_assignments_json"])
    if any(item not in by_id for item in ids):
        raise DomainError("UNKNOWN_EVIDENCE", "A bundle contributor assignment is missing")
    return [by_id[item] for item in ids]


def _prove(repo, store, assignment_id: str, evidence: ReviewEvidence, live: bool) -> dict:
    """Conditions 1-7 of the design. live=True (recording) also requires live bundle bytes and contributor gates;
    acceptance/verify leave currentness to the core bundle check so unrelated worktree edits stay irrelevant."""
    snapshot = store.read_snapshot()
    row = _row(snapshot["assignments"], assignment_id, "Review assignment")
    name, bound = binding_anchor(snapshot["events"], assignment_id)
    if name != REVIEW:
        raise DomainError("BLOCKED_EVIDENCE", "Assignment is not a bound bmad-review assignment")
    if row["permission_mode"] != "read-only" or row["state"] != "completed-claim" or not row["return_json"]:
        raise DomainError("BLOCKED_EVIDENCE", "Only a completed read-only review return can be a review")
    binding = read_binding(repo.common_dir / "coordinator", bound["evidence_ref"], bound["payload_digest"])
    if (binding.get("assignment_id"), binding.get("request_fingerprint")) != (row["id"], row["request_fingerprint"]):
        raise DomainError("UNKNOWN_EVIDENCE", "Review binding identity differs")
    historical_return(repo, store, assignment_id)  # anchor, receipt, launch binding, envelope, profile payload
    ret = json.loads(row["return_json"])
    if ret["source_id"] != evidence.source_id or ret["event_ref"] != evidence.source_ref or \
       evidence.source_ref != receipt_ref(run_name(row["id"], row["request_fingerprint"]), "return"):
        raise DomainError("UNKNOWN_EVIDENCE", "Review source is not the anchored return of this assignment")
    payload = read_receipt(repo, ret["event_ref"], ret["payload_digest"]).get("profile_payload")
    if not isinstance(payload, dict) or payload.get("valid") is not True:
        raise DomainError("UNKNOWN_EVIDENCE", "Return receipt carries no valid review payload")
    bundle, manifest = _bundle_row(snapshot, binding)
    if evidence.bundle_id != bundle["id"] or payload["bundle_id"] != bundle["id"]:
        raise DomainError("BLOCKED_EVIDENCE", "Review is not for the bound bundle")
    contributors = _contributors(snapshot, bundle)
    if evidence.reviewer_id != row["author_id"] or evidence.reviewer_id in manifest["derived_author_ids"] or \
       row["id"] in binding["contributor_assignment_ids"] or any(row["id"] == item["id"] for item in contributors):
        raise DomainError("BLOCKED_EVIDENCE", "Reviewer is not independent of the bundle contributors")
    threads = {json.loads(item["return_json"])["source_id"] for item in contributors if item["return_json"]}
    # Whole source ids across clients; the reviewer's prefix must be registered and equal its bound client.
    if clients.client_of(ret["source_id"]) != binding.get("client", clients.DEFAULT) or ret["source_id"] in threads or \
       ret["source_id"] in binding["contributor_source_ids"] or not threads:
        raise DomainError("BLOCKED_EVIDENCE", "Reviewer thread is not distinct from every contributor thread")
    if evidence.finding != _review_view(payload) or evidence.critical_open is not critical_open(payload):
        raise DomainError("BLOCKED_EVIDENCE", "Review finding or critical flag differs from the anchored receipt")
    if live:
        feature = _row(snapshot["features"], bundle["feature_id"], "Feature")
        worktree = _resolved_worktree(feature)
        if bundles.source_state(worktree, manifest) != "current":
            raise DomainError("STALE_INPUT", "Live bundle files differ from the sealed manifest")
        reasons = contract.check_binding(worktree, binding, store, feature) if "fingerprint_sha256" in binding else []
        reasons += Coordinator(store)._gate_reasons(snapshot, "bundle", contributors, None, feature,
                                                    spec=_bundle_spec(bundle, manifest))
        if reasons:
            raise DomainError("BLOCKED_EVIDENCE", "; ".join(reasons))
    return payload


class BoundReviewSource:
    """Classifies a review `observed` only when the whole bound proof holds; otherwise raises and records nothing."""

    one_review_per_receipt = True  # core record_review returns the stored review for a repeated anchored receipt

    def __init__(self, repo, store, assignment_id: str):
        self.repo, self.store, self.assignment_id = repo, store, assignment_id

    def classify_launch(self, event):
        return "reported"

    def classify_return(self, event):
        return "reported"

    def classify_review(self, event: ReviewEvidence):
        _prove(self.repo, self.store, self.assignment_id, event, live=True)
        return "observed"


def _evidence_from_row(review: dict) -> ReviewEvidence:
    finding = json.loads(review["finding_json"])
    return ReviewEvidence(review["bundle_id"], review["reviewer_id"], finding["source_id"], review["provenance_ref"],
                          finding["finding"], bool(review["critical_open"]))


def gate(stage: str, ctx: dict) -> list[str]:
    if stage in ("record", "bundle"):
        return []
    if ctx["worktree"] is None:
        return ["feature worktree is unresolved"]
    reasons = []
    try:
        if "fingerprint_sha256" in ctx["binding"]:   # a code review binding seals no contract (design D22)
            reasons += contract.check_binding(ctx["worktree"], ctx["binding"], ctx["store"], ctx["feature"])
        repo = resolve_repo(ctx["worktree"])
        if stage == "reserve":
            _, manifest = _bundle_row(ctx["store"].read_snapshot(), ctx["binding"])
            if bundles.source_state(ctx["worktree"], manifest) != "current":  # as review prepare and record
                raise DomainError("STALE_INPUT", "Live bundle files differ from the sealed manifest")
        else:
            _prove(repo, ctx["store"], ctx["assignment"]["id"], _evidence_from_row(ctx["review"]), live=False)
    except DomainError as exc:
        if exc.code == "STALE_INPUT":
            raise
        reasons.append(f"bound review proof failed: {exc.message}")
    except (OSError, KeyError, ValueError, TypeError) as exc:
        reasons.append(f"bound review proof is unverifiable: {exc}")
    return reasons


REVIEW_PROFILE = ReturnProfile(
    REVIEW,
    {"bundle_id": {"type": "string"}, "manifest_digest": {"type": "string"},
     "lenses_run": {"type": "array", "items": {"type": "string"}},
     "bundle_read": {"type": "array", "items": _text_object("path", "sha256")},
     "findings": {"type": "array", "items": _text_object(
         *FINDING_FIELDS[:-1], severity={"enum": list(SEVERITIES)})},
     "dispositions": {"type": "array", "items": _text_object(
         "contributor_id", kind={"enum": list(KINDS)}, index={"type": "integer"}, severity={"enum": list(SEVERITIES)})}},
    {"PYTHONDONTWRITEBYTECODE": "1"}, check, gate)
PROFILE = REVIEW_PROFILE  # profiles.get imports module.PROFILE


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_INPUT", message)


def _validate(spec: ReviewPrepare) -> None:
    if type(spec.client) is not str:
        raise _invalid("Worker client must be a registered client name")
    if not clients.get(spec.client).write_free_readonly:
        raise DomainError("UNSUPPORTED_CLIENT_MODE", f"Worker client {spec.client} has no write-free read-only mode")
    if not KEY.match(spec.key or "") or not spec.reviewer_id.strip():
        raise _invalid("Key and reviewer are required")
    if not spec.lenses or len(set(spec.lenses)) != len(spec.lenses) or any(not LENS.match(item) for item in spec.lenses):
        raise _invalid("Lenses must be distinct lower-case lens codes")


def _carried(repo, store, contributors: list[dict]) -> dict:
    """Every contributor assumption and partial marker, from validated historical receipts."""
    assumptions, flags = [], []
    for row in contributors:
        historical_return(repo, store, row["id"])
        ret = json.loads(row["return_json"])
        payload = read_receipt(repo, ret["event_ref"], ret["payload_digest"]).get("profile_payload") or {}
        if "bmad_status" not in payload:
            if binding_anchor(store.read_snapshot()["events"], row["id"])[0] in STORY_PROFILES:
                continue
            raise DomainError("BLOCKED_EVIDENCE", "A contributor has no planning payload to carry into the review")
        assumptions += [{"contributor_id": row["id"], "kind": "assumption", "index": index,
                         "text": json.dumps(item, sort_keys=True)} for index, item in enumerate(payload["assumptions"])]
        if payload["bmad_status"] == "partial":
            flags.append({"contributor_id": row["id"], "kind": "partial", "index": 0,
                          "text": "the contributor reported BMAD status partial; judge what is missing or unsafe"})
    return {"assumptions": assumptions, "critical_flags": flags}


def _quoted(values) -> str:
    return ", ".join(f"`{value}`" for value in values)


# The review return contract: exactly what `check` and `_exact` refuse, quoting their messages (attempt 4).
REVIEW_RULES = [
    "- Use result `completed`; a `blocked` or `needs-decision` return is never a review. Change no file (refused: a "
    "reviewer must not change files).",
    "- Copy `bundle_id` and `manifest_digest` verbatim from the profile line below (refused: bundle ID or manifest "
    "digest differs from the binding).",
    "- `lenses_run` lists each lens of the profile line exactly once and no other (refused: lenses run differ from the "
    "bound lens set).",
    "- `bundle_read` holds one object with exactly the string fields `path` and `sha256` per sealed file listed above: "
    "`path` is the first path of that line (the manifest path, e.g. `pkg/x.py`; the sealed location after `at` is "
    "where you read it and is accepted as an alias) and `sha256` the SHA-256 of the bytes you actually read (refused: "
    "bundle_read is malformed; bundle_read entry has the wrong fields; per-file digest attestations do not equal the "
    "sealed bundle manifest).",
    "- `findings` and `dispositions` are lists (refused: findings or dispositions are malformed). Each finding has "
    f"exactly the string fields {_quoted(FINDING_FIELDS)}; its `lens` is one of the profile line's lenses, verbatim, "
    f"and its `severity` one of {_quoted(SEVERITIES)} (refused: finding entry has the wrong fields; finding has a "
    "non-text field, an unknown severity or an unbound lens).",
    "- `dispositions` holds exactly one object `{contributor_id, kind, index, severity}` per carried item above, copied "
    f"verbatim: `contributor_id` a string, `kind` one of {_quoted(KINDS)}, `index` an integer and `severity` one of "
    f"{_quoted(SEVERITIES)} (refused: disposition entry has the wrong fields; disposition has a malformed field; "
    "dispositions must cover each carried contributor assumption and partial marker exactly once).",
]


# Smoke attempt 8: a defect tied to a decided RFC 8259 rule was rated `high`, so accept would have passed. The
# rubric names that case; the severity stays the reviewer's claim, and `critical_open` stays derived (never returned).
SEVERITY_RULES = [
    "- A finding that contradicts a mandate clause (for example a rule in a sealed `[dependency]` file, or a recorded "
    "or inherited decision above), or that makes an artifact's output invalid under a standard the mandate requires, "
    "IS `critical`, however narrow its trigger. Cite that clause id (the rule's id or heading, or the decision id) in "
    "the finding's `potential_consequence`.",
    "- A `critical` disposition of a carried item counts like a `critical` finding: either one sets the review's "
    "`critical_open`, which the coordinator derives (you never return it) and which blocks acceptance.",
]


def _planning_binding(store, events, assignment_id: str) -> dict:
    """The binding of a bundle contributor, whichever profile (planning or implementation) it is bound to."""
    name, bound = binding_anchor(events, assignment_id)
    if name not in (PLAN, *STORY_PROFILES) or bound is None:
        raise DomainError("BLOCKED_EVIDENCE", "Contributor is not a bound planning or implementation assignment")
    return read_binding(store.path.parent, bound["evidence_ref"], bound["payload_digest"])


def _provenance(store, snapshot, contributors) -> tuple[set[str], dict[str, list[str]]]:
    """(own, inherited): own are the decision ids that resolved a contributor's request or were passed to it at
    prepare; inherited maps decision ids to the accepted bundle ids named by the contributors' digest-checked
    bindings (`inherited_decisions`). One id may be both."""
    own = {item["id"] for item in resolving_decisions(snapshot, set(contributors))}
    found = {}
    for contributor in contributors:
        binding = _planning_binding(store, snapshot["events"], contributor)
        records = binding.get("inherited_decisions", [])
        if not isinstance(records, list) or any(not isinstance(item, dict) or set(item) != {"id", "bundle_id"} or
                                                not all(isinstance(value, str) for value in item.values())
                                                for item in records):
            raise DomainError("UNKNOWN_EVIDENCE", "A contributor's inherited decision record is malformed")
        for item in records:
            found.setdefault(item["id"], set()).add(item["bundle_id"])
        own |= {item.get("id") for item in binding.get("decisions", []) if isinstance(item, dict)} - \
            {item["id"] for item in records}
    return own, {key: sorted(value) for key, value in found.items()}


def _brief(spec: ReviewPrepare, worktree: Path, sealed: list[dict], carried: dict, bundle: dict,
           decisions: list[dict], own: set[str], inherited: dict[str, list[str]]) -> str:
    root, claude = spec.client_root, spec.client == clients.CLAUDE.name
    items = carried["assumptions"] + carried["critical_flags"]
    lines = ["# BMAD review of a sealed planning bundle (one attempt)", "",
             "You are a fresh, read-only reviewer. The coordinator holds all state; this brief is everything you have. "
             + client_label(spec.client), "", "## Read first",
             f"- `{root}/bmad-review/SKILL.md` and the lens files it names; run exactly these lenses: "
             + ", ".join(f"`{lens}`" for lens in spec.lenses), "",
             "## Sealed bundle (read only; read this copy, not the live worktree)"]
    lines += [f"- [{item['kind']}] `{item['path']}` at `{item['sealed_path']}`" for item in sealed]
    lines += ["", "## Carried items needing a disposition (each exactly once)"]
    lines += [f"- contributor `{item['contributor_id']}` kind `{item['kind']}` index {item['index']}: {item['text']}"
              for item in items] or ["- none"]
    # Rendered from the stored rows the sealed manifest names by digest; caller text never reaches this section.
    lines += ["", "## Recorded decisions (coordinator- or operator-recorded facts)",
              "These decisions resolved a contributor's question or were given to a contributor at prepare. They are "
              "recorded facts you check the artifacts against, not facts you can verify externally; their mere "
              "existence is not a finding."]
    lines += [f"- `{row['id']}` ({row['source_kind']}, `{row['source_ref']}`, mandate revision "
              f"{row['mandate_revision']}): {row['choice_json']}" for row in decisions
              if row["id"] in own or row["id"] not in inherited] or ["- none"]
    if any(row["id"] in inherited for row in decisions):
        lines += ["", "## Inherited decisions (sealed in an accepted bundle a contributor consumed)",
                  "These decisions were sealed in the accepted bundle named on each line, whose artifact a contributor "
                  "was given as an accepted input. They are recorded facts you check the artifacts against, not facts "
                  "you can verify externally; their mere existence is not a finding."]
        lines += [f"- `{row['id']}` ({row['source_kind']}, `{row['source_ref']}`, mandate revision "
                  f"{row['mandate_revision']}; accepted bundle " + ", ".join(f"`{item}`" for item in inherited[row["id"]])
                  + f"): {row['choice_json']}" for row in decisions if row["id"] in inherited]
    lines += ["", "## Severity rubric",
              "`critical` = unsafe for downstream use or contradicts the mandate; `high`, `medium`, `low` otherwise by "
              "consequence. BMAD review has no severity; you assign it here. Never lower a severity to make work pass.",
              *SEVERITY_RULES,
              "", "## Rules",
              "- Write nothing anywhere; " + ("your tool policy has no write tools and denies write-capable shell "
                                              "commands; every shell command is recorded and audited. It is a "
                                              "recorded policy, not OS confinement." if claude else
                                              "your sandbox is read-only.") +
              " Do not edit the live worktree or the sealed copy. Do not call any service or follow any handoff.",
              ("- " if claude else "- Set `PYTHONDONTWRITEBYTECODE=1` for every command. ") +
              "Do not start extra reviewers beyond what the installed review skill itself requires.",
              *(["- To hash the sealed files, run exactly `sha256sum <sealed path>…`; no env prefix, pipe, redirect "
                 "or chaining; `uv` is denied, so use the skill's documented fallback (read `customize.toml`).",
                 "- Use only Read, Grep, Glob and bare `sha256sum <sealed path>…`. Any other shell command, even a "
                 "read-only one that the CLI allows, makes your `bundle_read` a claim and fails the read gate."]
                if claude else []),
              "- Return the required generic fields plus `bundle_id`, `manifest_digest`, `lenses_run`, "
              "`bundle_read`, `findings` and `dispositions`:",
              *REVIEW_RULES,
              AFFECTED_IDS_RULE,
              "- Use result `blocked` with an explanation if you cannot read the sealed files; that is not a review.",
              "- Your statements about reading and understanding are claims, never proof.",
              "", "## Profile line", f"- bundle_id: `{bundle['id']}`",
              f"- manifest_digest: `{bundle['manifest_digest']}`", f"- sealed root: `{bundle['path']}`",
              f"- lenses: {', '.join(spec.lenses)}", ""]
    return "\n".join(lines)


def review_prepare(repo, store, owner, spec: ReviewPrepare) -> dict:
    """verify bundle/sealed copy/live bytes (and the contract for a planning bundle, design D22) -> brief -> create_assignment(required profile) -> seal -> bind."""
    _validate(spec)
    coordinator = Coordinator(store)
    snapshot = store.read_export_snapshot(owner)  # authenticates the owner first
    spec_sha256 = spec_digest(spec)
    prior_row = next((row for row in snapshot["assignments"] if row["idempotency_key"] == spec.key), None)
    if prior_row is not None:
        required, bound = _anchor_rows(snapshot["events"], prior_row["id"])  # an ambiguous anchor raises
        if required != REVIEW or (bound is not None and bound["source_id"] != REVIEW):
            raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key was used by a non-review assignment")
        if bound is not None:
            value = read_binding(repo.common_dir / "coordinator", bound["evidence_ref"], bound["payload_digest"])
            if value.get("spec_sha256") != spec_sha256:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Idempotency key was used with a different review spec")
            return {"assignment_id": prior_row["id"], "brief_ref": prior_row["brief_ref"],
                    "binding_sha256": bound["payload_digest"]}
        # required profile, no binding yet: a half-finished prepare that cannot have run; finish it below
    bundle = _row(snapshot["bundles"], spec.bundle_id, "Bundle")
    feature = _row(snapshot["features"], bundle["feature_id"], "Feature")
    if not feature["worktree"] or resolve_repo(Path(feature["worktree"])) != repo:
        raise DomainError("INVALID_REPOSITORY", "Review prepare must run from the feature's recorded worktree")
    worktree = repo.worktree
    if feature["mandate_revision"] != bundle["mandate_revision"]:
        raise DomainError("STALE_INPUT", "The bundle belongs to an earlier mandate revision")
    manifest = bundles.read_manifest(Path(bundle["path"]), bundle["manifest_digest"])
    if not bundles.sealed_copy(Path(bundle["path"]), manifest):
        raise DomainError("UNKNOWN_EVIDENCE", "Sealed bundle copy differs from its manifest")
    contributors = _contributors(snapshot, bundle)
    code = bool(manifest.get("commit"))   # design D22: a code bundle is reviewed without the planning contract
    # design D24: a commit-bound bundle of the spec author is a spec bundle
    spec_bundle = code and any(binding_anchor(snapshot["events"], row["id"])[0] == SPEC_AUTHOR for row in contributors)
    lenses, kind = (SPEC_LENSES, "spec") if spec_bundle else (CODE_LENSES, "code")
    if code and list(spec.lenses) != list(lenses):   # ruling R9, design D24: the review focus is fixed
        raise _invalid(f"A {kind} review runs exactly the lenses " + ", ".join(lenses))
    decided = sealed_decisions_state(manifest, snapshot["decisions"])
    if decided != "current":
        raise DomainError("STALE_INPUT" if decided == "stale" else "UNKNOWN_EVIDENCE",
                          "A sealed resolving decision changed or is missing")
    decisions = [next(row for row in snapshot["decisions"] if row["id"] == item["id"])
                 for item in manifest.get("decisions", [])]
    if bundles.source_state(worktree, manifest) != "current":
        raise DomainError("STALE_INPUT", "Live bundle files differ from the sealed manifest")
    if spec.reviewer_id in manifest["derived_author_ids"]:
        raise _invalid("The reviewer authored or edited this bundle")
    names = [binding_anchor(snapshot["events"], row["id"])[0] for row in contributors]
    if any(name not in (PLAN, *STORY_PROFILES) for name in names):
        raise DomainError("BLOCKED_EVIDENCE", "Review needs bound bmad-planning or story (implementation, spec author) "
                          "contributors")
    if any((name in STORY_PROFILES) != bool(manifest.get("commit")) for name in names):
        raise DomainError("BLOCKED_EVIDENCE", "Implementation contributors need a commit-bound bundle, and a "
                          "commit-bound bundle needs implementation contributors")
    if code and spec.compat_decision_id is not None:
        raise _invalid("A code review takes no compatibility decision")
    if not code and not spec.compat_decision_id:
        raise _invalid("A planning review needs a compatibility decision")
    report = None
    if not code:
        report = contract.discover(worktree, spec.client_root)
        if not report.supported:
            raise DomainError("UNSUPPORTED_CONTRACT", "; ".join(report.reasons))
        # Drift since the contributors were bound is STALE_INPUT (design section 9); an unjudged but unchanged
        # fingerprint stays BLOCKED_EVIDENCE, exactly as in planning prepare.
        if any(_planning_binding(store, snapshot["events"], row["id"])["fingerprint_sha256"] != report.fingerprint_sha256
               for row in contributors):
            raise DomainError("STALE_INPUT", "BMAD contract differs from the one the bundle contributors were bound to")
        decision = next((row for row in snapshot["decisions"] if row["id"] == spec.compat_decision_id), None)
        if decision is None or contract.compat_decision({"decisions": [decision]}, feature["id"],
                                                        feature["mandate_revision"],
                                                        report.fingerprint_sha256) != decision["id"]:
            raise DomainError("BLOCKED_EVIDENCE", "Compatibility decision does not judge this fingerprint at the "
                              "current mandate revision")
    reasons = coordinator._gate_reasons(snapshot, "bundle", contributors, None, feature,
                                        spec=_bundle_spec(bundle, manifest))
    if reasons:
        raise DomainError("BLOCKED_EVIDENCE", "; ".join(reasons))
    carried = _carried(repo, store, contributors)
    sealed = _sealed_files(bundle, manifest)
    if code:
        from . import bmad_implementation as implementation
        project = _planning_binding(store, snapshot["events"], contributors[0]["id"]).get("project")
        brief_ref = f"{implementation._briefs_dir(project)}/{spec.key}.md"
    else:
        brief_ref = f"{contract.central_path(worktree, 'core.output_folder')}/coordinator/briefs/{spec.key}.md"
    sources = [_file_entry(worktree, item["path"]) for item in sealed]
    refuse_while_running(store.read_snapshot(), worktree)
    own, inherited = _provenance(store, snapshot, manifest["source_assignment_ids"])
    if spec_bundle:
        from . import bmad_spec_author
        text = bmad_spec_author.review_brief(spec, worktree, sealed, bundle, feature, manifest, decisions, carried,
                                             store, repo, snapshot)
    elif code:
        text = implementation.review_brief(spec, worktree, sealed, bundle, feature, manifest, decisions, carried, store,
                                           repo, snapshot)
    else:
        text = _brief(spec, worktree, sealed, carried, bundle, decisions, own, inherited)
    brief = _write_brief(worktree, brief_ref, text)
    contract_files = [] if code else report.contract_files
    assignment_id = coordinator.create_assignment(owner, AssignmentInput(
        feature["id"], spec.key, brief_ref, feature["mandate_revision"], [brief, *sources], contract_files,
        _sha(canonical(contract_files)), [], "read-only", spec.reviewer_id, [], []), required_profile=REVIEW)
    value = {"kind": "binding", "format": 1, "profile": REVIEW, "role": "review", "assignment_id": assignment_id,
             "request_fingerprint": coordinator.detail("assignment", assignment_id)["request_fingerprint"],
             "worktree": str(worktree), "common_dir": str(repo.common_dir), "client_root": spec.client_root,
             "spec_sha256": spec_sha256, "bundle_id": bundle["id"],
             "manifest_digest": bundle["manifest_digest"], "sealed_root": bundle["path"], "sealed_files": sealed,
             "contributor_assignment_ids": [row["id"] for row in contributors],
             "contributor_source_ids": sorted(json.loads(row["return_json"])["source_id"] for row in contributors),
             "lenses": list(spec.lenses), "brief_sha256": brief["digest"], "carried": carried, **client_binding(spec)}
    if not code:   # a planning review binding seals the contract it was prepared against
        value.update(fingerprint_sha256=report.fingerprint_sha256, contract=report.contract,
                     compat_decision_id=spec.compat_decision_id)
    evidence = seal_binding(repo.common_dir / "coordinator", value)
    coordinator.bind_assignment(owner, assignment_id, evidence)
    return {"assignment_id": assignment_id, "brief_ref": brief_ref, "binding_sha256": evidence.payload_digest}


def record_bound_review(repo, store, owner, assignment_id: str) -> dict:
    """Builds the ReviewEvidence from the anchored receipt itself; a retry returns the stored review."""
    snapshot = store.read_export_snapshot(owner)
    row = _row(snapshot["assignments"], assignment_id, "Review assignment")
    name, bound = binding_anchor(snapshot["events"], assignment_id)
    if name != REVIEW:
        raise DomainError("BLOCKED_EVIDENCE", "Assignment is not a bound bmad-review assignment")
    if row["state"] != "completed-claim" or not row["return_json"]:
        raise DomainError("BLOCKED_EVIDENCE", "A blocked, undecided or unfinished reviewer return is never a review")
    binding = read_binding(repo.common_dir / "coordinator", bound["evidence_ref"], bound["payload_digest"])
    ret = json.loads(row["return_json"])
    payload = read_receipt(repo, ret["event_ref"], ret["payload_digest"]).get("profile_payload")
    if not isinstance(payload, dict) or payload.get("valid") is not True:
        raise DomainError("UNKNOWN_EVIDENCE", "Return receipt carries no valid review payload")
    evidence = ReviewEvidence(binding["bundle_id"], row["author_id"], ret["source_id"], ret["event_ref"],
                              _review_view(payload), critical_open(payload))
    review_id = Coordinator(store, BoundReviewSource(repo, store, assignment_id)).record_review(owner, evidence)
    return {"id": review_id, "provenance_class": "observed", "critical_open": evidence.critical_open}


def bundle_for_workspace(repo, store, owner, request: BundleRequest) -> dict:
    """Thin wrapper over core capture_bundle: artifacts are the observed claimed files under the workspace,
    contributors are all assignments that touched them, and every required project dependency is unioned in."""
    snapshot = store.read_export_snapshot(owner)
    feature = _row(snapshot["features"], request.feature_id, "Feature")
    worktree = _resolved_worktree(feature)
    if not isinstance(request.workspace, str) or request.workspace.endswith("/"):
        raise _invalid("Workspace must be a relative directory without a trailing slash")
    _safe_feature_paths(worktree, [request.workspace])
    prefix = request.workspace + "/"
    if not isinstance(request.dependency_paths, list) or any(not isinstance(path, str) for path in request.dependency_paths):
        raise _invalid("Dependency paths must be a list of relative file names")
    if any(path.startswith(prefix) for path in request.dependency_paths):
        raise _invalid("Files inside the workspace are artifacts, not dependencies")
    claims, touching = {}, []
    for row in snapshot["assignments"]:
        if row["feature_id"] != feature["id"] or not row["return_json"]:
            continue
        returned = json.loads(row["return_json"])
        if returned["provenance_class"] != "observed":
            continue
        for entry in returned["changed_paths"]:
            path = entry.get("canonical_path") or _path_identity(worktree, entry["path"])
            if path.startswith(prefix):
                claims.setdefault(path, set()).add(entry["digest"])
                touching.append(row)
    artifacts = sorted(claims)
    if not artifacts:
        raise DomainError("BLOCKED_EVIDENCE", "No observed claimed file exists under the workspace")
    for path in artifacts:
        live = worktree / path
        if live.is_symlink() or not live.is_file() or _sha(live.read_bytes()) not in claims[path]:
            raise DomainError("BLOCKED_EVIDENCE", f"Claimed workspace file is missing or matches no observed claim: {path}")
    contract_files_json = touching[-1]["contract_files_json"]
    contributors = _artifact_contributors(snapshot["assignments"], snapshot["decision_requests"], feature["id"],
                                          worktree, {path: _sha((worktree / path).read_bytes()) for path in artifacts},
                                          feature["mandate_revision"], contract_files_json)
    contract_files = json.loads(contract_files_json)
    sourced = sorted({item["path"] for row in snapshot["assignments"] if row["id"] in contributors
                      for item in json.loads(row["source_digests_json"]) if item["path"].startswith(prefix)}
                     - set(artifacts))
    if sourced:
        raise DomainError("BLOCKED_EVIDENCE", "Workspace files given to a contributor are not observed artifacts: "
                          + ", ".join(sourced))
    required = {}
    for row in snapshot["assignments"]:
        # An unbound contributor has no frozen dependencies; the core bundle gate refuses the mixed set.
        if row["id"] in contributors and binding_anchor(snapshot["events"], row["id"])[0] == PLAN:
            binding = read_assignment_binding(store, repo, row["id"])
            for path, digest in effective_dependencies(store, snapshot, row, binding, contributors).items():
                if required.setdefault(path, digest) != digest:
                    raise DomainError("BLOCKED_EVIDENCE", f"Contributors froze different bytes for dependency {path}")
    skipped = set(artifacts) | {item["path"] for item in contract_files}
    dependencies = sorted((set(request.dependency_paths) | set(required)) - skipped)
    bundle_id = Coordinator(store).capture_bundle(owner, BundleInput(
        feature["id"], artifacts, dependencies, sorted(contributors), contract_files))
    return {"id": bundle_id, "artifact_paths": artifacts, "dependency_paths": dependencies,
            "source_assignment_ids": sorted(contributors)}


def accept_bound(store, owner, bundle_id: str, review_id: str) -> dict:
    """Wrapper over core accept; every gate (bound review proof, contributors, contract, dependencies) lives there."""
    return {"id": Coordinator(store).accept(owner, bundle_id, review_id)}


def verify_bmad(store) -> dict:
    """Core verify with the reasons per acceptance spelled out."""
    report = Coordinator(store).verify()
    checked = [{**item, "reasons": [] if item["current"] == "current" else
                list(item.get("gate") or []) or [item.get("reason", "unverifiable")]} for item in report.checked]
    return {"historical_accepted": report.historical_accepted, "current": report.current, "checked": checked}
