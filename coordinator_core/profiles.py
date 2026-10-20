"""Closed registry of return profiles; the generic profile is the unchanged nine-field return."""

import importlib
from dataclasses import dataclass
from typing import Callable

from .model import DomainError


@dataclass(frozen=True)
class ReturnProfile:
    name: str
    extra: dict[str, dict]      # JSON-schema fragments added (all required) to the generic fields
    env: dict[str, str]         # extra launch env, recorded in launch.json
    check: Callable[..., dict]  # (final, assignment, binding, run_dir, observed_changed) -> payload; raises UNKNOWN_EVIDENCE
    gate: Callable[[str, dict], list[str]]  # (stage, ctx) -> blocking reasons; [] = pass
    hint: Callable[[dict], str] | None = None  # binding -> extra return guidance for worker-file envelopes only


STAGES = ("reserve", "bundle", "record", "accept", "verify")
MODULES = {"bmad-planning-v1": "bmad_planning", "bmad-review-v1": "bmad_review",
           "bmad-implementation-v1": "bmad_implementation", "bmad-spec-author-v1": "bmad_spec_author"}
GENERIC = ReturnProfile("generic", {}, {}, lambda *args: {}, lambda stage, ctx: [])
REGISTRY: dict[str, ReturnProfile] = {}  # tests may inject; production code never does


def get(name: str | None) -> ReturnProfile:
    if name is None:
        return GENERIC
    if name not in MODULES:
        raise DomainError("INVALID_INPUT", "Unknown return profile")
    if name in REGISTRY:
        return REGISTRY[name]
    module = f"{__package__}.{MODULES[name]}"
    try:
        profile = importlib.import_module(module).PROFILE
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        raise DomainError("UNKNOWN_EVIDENCE", f"Return profile {name} is not installed") from exc
    if profile.name != name:
        raise DomainError("CORRUPT_STATE", "Profile module registers a different name")
    return profile
