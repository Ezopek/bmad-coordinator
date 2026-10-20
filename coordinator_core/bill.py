"""The bill (bill design B1–B4): cost and time accounting per launch, story, lease and epic, derived on read from the
sealed receipts and the store, never stored. The coordinator's observations (launches, wall clock, driver turns,
runs) stay apart from the clients' reports (tokens, USD, premium requests, client turns), and every sum carries the
count of launches that contributed nothing to it (`unreported`). No price of our own (B7): money appears only where a
client gave a figure."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .model import DomainError
from .service import ANCHOR_TOPS, _anchored_bytes
from .store import open_store

FORMAT = 1
TOPS = ("runs", "bindings") + ANCHOR_TOPS   # the receipts the bill reads, every one digest-checked against its event
TOKENS = ("input", "cached_input", "cache_write", "output", "reasoning")
# the clients' token keys mapped to the bill's vocabulary (B2); a key a client lacks contributes nothing
TOKEN_KEYS = {
    "claude-code": ("usage", {"input_tokens": "input", "cache_read_input_tokens": "cached_input",
                              "cache_creation_input_tokens": "cache_write", "output_tokens": "output"}),
    "codex-cli": ("usage", {"input_tokens": "input", "cached_input_tokens": "cached_input",
                            "cache_write_input_tokens": "cache_write", "output_tokens": "output",
                            "reasoning_output_tokens": "reasoning"}),
    "copilot": ("session_usage", {"inputTokens": "input", "cacheReadTokens": "cached_input",
                                  "cacheWriteTokens": "cache_write", "outputTokens": "output",
                                  "reasoningTokens": "reasoning"}),
}
LAUNCH_KINDS = ("launch", "judge")


def _sealed(base: Path, ref: str, digest: str) -> dict | None:
    """One anchored receipt, digest-checked; None when missing, changed or not an object."""
    try:
        value = json.loads(_anchored_bytes(base, ref, digest, TOPS))
    except (DomainError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _number(value) -> int | float | None:
    return value if type(value) in (int, float) and value == value else None


def _instant(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _tokens(client: str, facts: dict) -> dict | None:
    """The client's token counts in the bill's vocabulary, None when the facts carry none."""
    source, keys = TOKEN_KEYS.get(client, (None, {}))
    usage = facts.get(source) if source else None
    if client == "copilot" and isinstance(usage, dict):   # B2: {"usage": {...}, "requests": ...}
        usage = usage.get("usage")
    if not isinstance(usage, dict):
        return None
    found = {name: _number(usage.get(key)) for key, name in keys.items()}
    found = {name: value for name, value in found.items() if value is not None}
    return found or None


def reported(client: str, facts: dict | None) -> dict | None:
    """The client's own figures from the sealed `client_facts`, copied, never derived; None without a return."""
    if facts is None:
        return None
    facts = facts if isinstance(facts, dict) else {}
    usage = facts.get("usage") if isinstance(facts.get("usage"), dict) else {}
    return {"client_turns": _number(facts.get("num_turns")), "tokens": _tokens(client, facts),
            "usd": _number(facts.get("total_cost_usd")) if client == "claude-code" else None,
            "premium_requests": _number(usage.get("premiumRequests")) if client == "copilot" else None,
            "client_facts": facts}


def _placements(base: Path, events: list) -> dict[str, dict]:
    """Where the driver launched each assignment or judgment: {id: {lease_id, run_id, step_id, role}}, from the
    sealed turn records (the driver's own account of its actions)."""
    found = {}
    for row in events:
        if row["kind"] != "drive.turn":
            continue
        turn = _sealed(base, row["evidence_ref"], row["payload_digest"])
        action = (turn or {}).get("action")
        if not isinstance(action, dict) or action.get("kind") not in LAUNCH_KINDS:
            continue
        key = action.get("assignment_id") if action["kind"] == "launch" else action.get("judgment_key")
        if isinstance(key, str):
            found[key] = {"lease_id": row["subject_id"], "run_id": row["source_id"], "step_id": action.get("step_id"),
                          "role": "judgment" if action["kind"] == "judge" else action.get("role")}
    return found


def _flavour(binding: dict | None) -> str | None:
    """What the attempt continues, from its binding: a correction, a continuation, or the first attempt."""
    continues = (binding or {}).get("continues")
    if not isinstance(continues, dict):
        return None
    if continues.get("corrects_review") or isinstance(continues.get("after_correction"), dict) and \
       not continues.get("decision") and not continues.get("client_stop"):
        return "correction"
    return "continuation"


def rows(base: Path, snapshot: dict) -> list[dict]:
    """Every client launch the store anchored, in time order (B1): worker and reviewer runs from
    `assignment.launch-observed` and `assignment.return-observed`, judgments from `judgment.returned`."""
    events = snapshot["events"]
    stories = {row["id"]: (row["branch"] or "").removeprefix("story/") or None for row in snapshot["features"]}
    feature_of = {row["id"]: row["feature_id"] for row in snapshot["assignments"]}
    feature_of.update({row["id"]: row["feature_id"] for row in snapshot["decision_requests"]})
    placed = _placements(base, events)
    bindings = {row["subject_id"]: row for row in events if row["kind"] == "assignment.bound"}
    returns = {row["subject_id"]: row for row in events if row["kind"] == "assignment.return-observed"}
    found = []
    for row in events:
        if row["kind"] == "assignment.launch-observed":
            assignment = row["subject_id"]
            launch = _sealed(base, row["evidence_ref"], row["payload_digest"])
            ret = returns.get(assignment)
            value = _sealed(base, ret["evidence_ref"], ret["payload_digest"]) if ret else None
            place = placed.get(assignment, {})
            binding = bindings.get(assignment)
            feature = feature_of.get(assignment)
            found.append(_row(launch, value, ret is not None, feature, stories.get(feature), place,
                              _flavour(_sealed(base, binding["evidence_ref"], binding["payload_digest"]) if binding
                                       else None),
                              {"assignment_id": assignment}))
        elif row["kind"] == "judgment.returned":
            key = row["subject_id"]
            record = _sealed(base, row["evidence_ref"], row["payload_digest"]) or {}
            receipts = record.get("receipts") if isinstance(record.get("receipts"), dict) else {}
            launch = _sealed(base, f"judgments/{key}/receipts/launch.json", receipts.get("launch.json", "")) \
                if "launch.json" in receipts else None
            value = _sealed(base, f"judgments/{key}/receipts/return.json", receipts.get("return.json", "")) \
                if "return.json" in receipts else None
            last = _sealed(base, f"judgments/{key}/receipts/last.json", receipts.get("last.json", "")) \
                if "last.json" in receipts else None
            feature = feature_of.get(record.get("request_id"))
            place = placed.get(key, {"role": "judgment"})
            found.append(_row(launch, value, "return.json" in receipts, feature, stories.get(feature),
                              {**place, "role": "judgment"}, None,
                              {"judgment_key": key, "verdict": (last or {}).get("verdict") or record.get("state")}))
    found.sort(key=lambda item: (item["started_at"] or "", item.get("assignment_id") or item.get("judgment_key")))
    return found


def _row(launch, value, returned: bool, feature, story, place: dict, flavour, ids: dict) -> dict:
    launch = launch if isinstance(launch, dict) else None
    value = value if isinstance(value, dict) else None
    client = (launch or value or {}).get("client")
    state = "unknown" if launch is None or (returned and value is None) else "observed" if value else "no-return"
    started, ended = (launch or {}).get("observed_at"), (value or {}).get("ended_at")
    wall = None
    if _instant(started) and _instant(ended):
        wall = round((_instant(ended) - _instant(started)).total_seconds(), 3)
    role = place.get("role") or ((launch or {}).get("requested_mode") == "read-only" and "reviewer") or "worker"
    outcome = value.get("result") if value and value.get("result") else \
        ("client-stopped" if value and value.get("client_stop") else None)
    if ids.get("verdict") is not None:
        outcome = ids.pop("verdict")
    return {**ids, "state": state, "started_at": started, "ended_at": ended, "story": story, "feature_id": feature,
            "lease_id": place.get("lease_id"), "run_id": place.get("run_id"), "role": role,
            "step_id": place.get("step_id"), "attempt": flavour, "client": client, "model": (launch or {}).get("model"),
            "outcome": outcome, "observed": {"wall_seconds": wall},
            "reported": reported(client, value.get("client_facts", {})) if state == "observed" else None}


def _runs(base: Path, snapshot: dict, lease_ids: set[str]) -> dict:
    """The leases' runs and driver turns from their anchored summaries (observed)."""
    runs = turns = 0
    for row in snapshot["events"]:
        if row["subject_id"] not in lease_ids:
            continue
        if row["kind"] == "drive.started":
            runs += 1
        elif row["kind"] in ("drive.ended", "drive.stop-acknowledged"):
            summary = _sealed(base, row["evidence_ref"], row["payload_digest"]) or {}
            turns += _number((summary.get("counts") or {}).get("turns")) or 0
    return {"runs": runs, "driver_turns": turns}


def aggregate(items: list[dict], runs: dict | None = None) -> dict:
    """Observed, reported and unreported sums over rows (B3); a sum is never without its unreported count."""
    observed = {"launches": len(items), "wall_seconds": round(sum(item["observed"]["wall_seconds"] or 0
                                                                   for item in items), 3), **(runs or {})}
    usd = premium = 0.0
    tokens = {name: 0 for name in TOKENS}
    unreported = {"launches": 0, "usd": 0, "premium_requests": 0, "tokens": 0}
    for item in items:
        rep = item["reported"]
        if rep is None:
            unreported["launches"] += 1
            for name in ("usd", "premium_requests", "tokens"):
                unreported[name] += 1
            continue
        if rep["usd"] is None:
            unreported["usd"] += 1
        else:
            usd += rep["usd"]
        if rep["premium_requests"] is None:
            unreported["premium_requests"] += 1
        else:
            premium += rep["premium_requests"]
        if rep["tokens"] is None:
            unreported["tokens"] += 1
        else:
            for name, value in rep["tokens"].items():
                tokens[name] += value
    reported_sum = {"usd": round(usd, 6), "premium_requests": premium if premium != int(premium) else int(premium),
                    "tokens": tokens}
    return {"observed": observed, "reported": reported_sum, "unreported": unreported}


def _grouped(items: list[dict], names: tuple[str, ...], runs_of=None) -> list[dict]:
    """The rows grouped by the named fields, in first-seen order, each group with its aggregate."""
    groups: dict = {}
    for item in items:
        groups.setdefault(tuple(item[name] for name in names), []).append(item)
    return [{**dict(zip(names, key)), **aggregate(members, runs_of(members) if runs_of else None)}
            for key, members in groups.items()]


def scoped(base: Path, snapshot: dict, *, feature_ids: list[str] | None = None, lease_id: str | None = None,
           stories_order: list[str] | None = None) -> dict:
    """The bill of a scope: rows, the aggregates by story, role and client/model, the totals."""
    items = rows(base, snapshot)
    if feature_ids is not None:
        items = [item for item in items if item["feature_id"] in feature_ids]
    if lease_id is not None:
        items = [item for item in items if item["lease_id"] == lease_id]
    leases = {item["lease_id"] for item in items if item["lease_id"]}
    if lease_id is not None:
        leases = {lease_id}
    by_story = _grouped(items, ("story", "feature_id"),
                        lambda members: _runs(base, snapshot, {m["lease_id"] for m in members if m["lease_id"]}))
    if stories_order:
        by_story.sort(key=lambda entry: stories_order.index(entry["feature_id"])
                      if entry["feature_id"] in stories_order else len(stories_order))
    return {"kind": "bill", "format": FORMAT, "launches": items, "by_story": by_story,
            "by_role": _grouped(items, ("role",)),
            "by_client_model": _grouped(items, ("client", "model")),
            "totals": aggregate(items, _runs(base, snapshot, leases))}


def summary(base: Path, snapshot: dict, feature_ids: list[str], lease_id: str | None = None,
            run_id: str | None = None) -> dict:
    """The aggregate alone, for the drive and epic summaries (B4); a run's bill counts its own launches only."""
    items = [item for item in rows(base, snapshot) if item["feature_id"] in feature_ids and
             (lease_id is None or item["lease_id"] == lease_id) and (run_id is None or item["run_id"] == run_id)]
    leases = {lease_id} if lease_id else {item["lease_id"] for item in items if item["lease_id"]}
    runs = None if run_id else _runs(base, snapshot, leases)
    return aggregate(items, runs)


def result_line(total: dict) -> str:
    """One line for the spec's `## Coordinator Result` (B4): the observed part, then what the clients reported, every
    absent quantity left out; the claim stays marked as the clients'."""
    observed, reported, unreported = total["observed"], total["reported"], total["unreported"]
    def launches(n: int) -> str:
        return f"{n} launch" + ("" if n == 1 else "es")
    parts = [launches(observed["launches"]), f"{int(round(observed['wall_seconds']))} s client wall clock"]
    claims = []
    if unreported["usd"] < observed["launches"]:
        claims.append(f"USD {reported['usd']:.2f}" + (f" ({launches(unreported['usd'])} without a figure)"
                                                      if unreported["usd"] else ""))
    if unreported["premium_requests"] < observed["launches"]:
        claims.append(f"{reported['premium_requests']} premium requests")
    if unreported["tokens"] < observed["launches"]:
        tokens = reported["tokens"]
        claims.append(f"{tokens['input'] + tokens['cached_input'] + tokens['cache_write']} input and "
                      f"{tokens['output'] + tokens['reasoning']} output tokens"
                      + (f" ({launches(unreported['tokens'])} without counts)" if unreported["tokens"] else ""))
    text = ", ".join(parts)
    return text + ("; reported by the clients: " + ", ".join(claims) if claims else "; nothing reported by the clients")


def usd_reported(base: Path, snapshot: dict, feature_ids: list[str]) -> tuple[float, int]:
    """(the reported USD over the features' launches, the launches without a figure), for the bound (B5, B6)."""
    items = [item for item in rows(base, snapshot) if item["feature_id"] in feature_ids]
    total = aggregate(items)
    return total["reported"]["usd"], total["unreported"]["usd"]


def command(repo, *, epic: str | None = None, feature: str | None = None, lease: str | None = None) -> dict:
    """`bill [--epic ID | --feature ID | --lease ID]`: read only, no token, no lock (B3)."""
    if sum(item is not None for item in (epic, feature, lease)) > 1:
        raise DomainError("INVALID_INPUT", "bill takes one of --epic, --feature or --lease")
    try:
        store = open_store(repo)
    except DomainError as exc:
        if exc.code == "UNINITIALIZED":
            return {"state": "uninitialized"}
        raise
    snapshot, base = store.read_snapshot(), store.path.parent
    scope: dict = {"repository_id": snapshot["repository_id"]}
    if epic is not None:
        from .drive_epic import newest_state
        state = newest_state(store, epic, snapshot)
        if state is None:
            raise DomainError("NOT_FOUND", "No such epic")
        order = [story["feature_id"] for story in state.get("stories", []) if story.get("feature_id")]
        scope.update(epic_id=epic, epic=state.get("epic"), stories=[story["key"] for story in state.get("stories", [])
                                                                      if story.get("feature_id")])
        return {**scoped(base, snapshot, feature_ids=order, stories_order=order), "scope": scope}
    if feature is not None:
        if feature not in {row["id"] for row in snapshot["features"]}:
            raise DomainError("NOT_FOUND", "No such feature")
        return {**scoped(base, snapshot, feature_ids=[feature]), "scope": {**scope, "feature_id": feature}}
    if lease is not None:
        leased = [row for row in snapshot["events"] if row["kind"] == "drive.leased" and row["subject_id"] == lease]
        if not leased:
            raise DomainError("NOT_FOUND", "No such lease")
        return {**scoped(base, snapshot, lease_id=lease),
                "scope": {**scope, "lease_id": lease, "feature_id": leased[-1]["source_id"]}}
    return {**scoped(base, snapshot), "scope": scope}
