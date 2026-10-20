"""The backlog (backlog design A1): what a layout must answer for the drives, in the coordinator's own words, and the
closed registry of layouts (A3). The BMAD layout, `bmad`, is the one shipped (A2); its code lives in `backlog_bmad`,
moved there unchanged, and this adapter only names it. A second layout implements `Backlog`, registers under its
kind and passes `tests/test_backlog_contract.py` (A4)."""
from __future__ import annotations

from . import backlog_bmad
from .model import DomainError

DEFAULT = "bmad"


class Backlog:
    """One layout of the backlog. Every reader takes a commit: the drives never read the working tree for the
    backlog. `artifacts_dir` is the project block's status files' directory (`project.artifacts_dir`)."""
    kind: str = ""

    def stories(self, repo, commit: str, epics_path: str, epic: int) -> list[dict]:
        """The epic's stories in the backlog's order: `{number, title, key, keys, status}` each; `key` None and
        `keys` the candidates when the status map has none or several for the story."""
        raise NotImplementedError

    def entry(self, repo, commit: str, key: str, epics_path: str, spec_path: str):
        """The story's entry for the spec author (title, user story lines, acceptance lines, precondition bullets,
        the entry's text), refused when the key is not listed at backlog or ready, has no acceptance lines, or the
        spec exists at the commit."""
        raise NotImplementedError

    def entry_of_number(self, repo, commit: str, epics_path: str, number: str):
        """The entry by its number (`<epic>.<m>`), for the epic's precondition gate."""
        raise NotImplementedError

    def preconditions(self, repo, commit: str, artifacts_dir: str, bullets) -> list[dict]:
        """The mechanical state of each precondition bullet: `met`, or `unmet` with a reason, and the lines for a
        brief. Never raises on the ledger's content."""
        raise NotImplementedError

    def precondition_lines(self, found: list[dict], commit: str) -> list[str]:
        """The brief's precondition section from `preconditions`' rows."""
        raise NotImplementedError

    def status_paths(self, artifacts_dir: str) -> dict[str, str]:
        """`{"sprint": <the status map file>, "ledger": <the deferred decisions file>}` under `artifacts_dir`."""
        raise NotImplementedError

    def edit_status(self, text: str, key: str, status: str) -> str:
        """The status map with the story at `status` (and the epic moved as the layout's rule says), comments, order,
        a BOM and CRLF preserved."""
        raise NotImplementedError

    def append_ledger(self, text: str, entries) -> str:
        """The ledger with the canonical entries appended, numbered after the greatest existing one."""
        raise NotImplementedError

    def protected(self) -> tuple[str, ...]:
        """Directories a worker may never write, beside the coordinator's own."""
        return ()


class BmadBacklog(Backlog):
    """The BMAD layout (backlog design A2): `epics.md` with `### Story <N>.<M>:` entries, `sprint-status.yaml` with
    its `development_status` map, `deferred-work.md` with `### DW-<n>:` entries, under `artifacts_dir`."""
    kind = "bmad"
    default_artifacts_dir = backlog_bmad.ARTIFACTS_DIR

    def stories(self, repo, commit, epics_path, epic):
        return backlog_bmad.plan_stories(repo, commit, epics_path, epic)

    def entry(self, repo, commit, key, epics_path, spec_path):
        return backlog_bmad._epic_entry(repo, commit, key, epics_path, spec_path)

    def entry_of_number(self, repo, commit, epics_path, number):
        return backlog_bmad.parse_epic_story(backlog_bmad._read_at(repo, commit, epics_path, "Epics file"), number)

    def preconditions(self, repo, commit, artifacts_dir, bullets):
        return backlog_bmad.preconditions_state(repo, commit, artifacts_dir, bullets)

    def precondition_lines(self, found, commit):
        return backlog_bmad.preconditions_section(found, commit)

    def status_paths(self, artifacts_dir):
        return {"sprint": f"{artifacts_dir}/sprint-status.yaml", "ledger": f"{artifacts_dir}/{backlog_bmad._LEDGER}"}

    def edit_status(self, text, key, status):
        return backlog_bmad.sync_sprint_status(text, key, status)

    def append_ledger(self, text, entries):
        return backlog_bmad.append_deferred_work(text, entries)

    def protected(self):
        return ("_bmad/", "_bmad-output/", ".bmad-loop/")


REGISTRY: dict[str, Backlog] = {BmadBacklog.kind: BmadBacklog()}   # closed (A3); tests may inject, production never
KINDS = tuple(REGISTRY)


def get(kind: str | None) -> Backlog:
    """The adapter of a kind; None is the default. An unknown kind is `INVALID_INPUT`."""
    name = DEFAULT if kind is None else kind
    found = REGISTRY.get(name) if isinstance(name, str) else None
    if found is None:
        raise DomainError("INVALID_INPUT", f"backlog kind {name!r} is not supported (one of {', '.join(KINDS)})")
    return found


def kind_of(project: dict | None) -> str:
    """The kind a pipeline's project block names (`project.backlog.kind`), else the default."""
    block = (project or {}).get("backlog")
    return block.get("kind", DEFAULT) if isinstance(block, dict) else DEFAULT


def resolve(project: dict | None) -> Backlog:
    return get(kind_of(project))
