"""The BMAD layout of the backlog (backlog design A2): today's parsing and editing of the epics file, the sprint status
map and the deferred-work ledger, moved here unchanged from `bmad_implementation` and `drive_epic`, with the text and
Git helpers they share. `backlog.BmadBacklog` wraps them as the `bmad` adapter; the old modules import these names
back, so nothing that pinned their bytes changed."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .model import DomainError
from .repo import GIT_SAFE


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_INPUT", message)


READY = "ready-for-dev"


ARTIFACTS_DIR = "_bmad-output/implementation-artifacts"   # the project key `artifacts_dir` overrides it


_LEDGER, _ARCHIVE = "deferred-work.md", "deferred-work-archive.md"


_STORY_HEADING = re.compile(r"^### Story (\d+)\.(\d+): (.+?)\s*$", re.M)


_SECTION_END = re.compile(r"^#{1,3} ", re.M)   # the next story, or the next epic's heading


_DW = re.compile(r"^### DW-(\d+):", re.M)


_STATUS_LINE = re.compile(r"^(\s+)([^\s:#][^:]*?):\s*([^\s#]+)\s*$")


_DW_ID = re.compile(r"\bDW-(\d+)\b")


_FIELD = re.compile(r"^([a-z][a-z0-9_-]*):(?: (.*))?$")


_DECISION_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\s+|$)")


_LABEL_MARKUP = re.compile(r"^[\W_]+")   # E6: punctuation, markup and spaces before a decision's option label


def _shape(message: str) -> DomainError:
    return DomainError("STALE_INPUT", "status-shape: " + message)


def _plain(text: str) -> tuple[str, str, str]:
    """A status file's BOM, its line ending, and its text without the BOM and with LF line endings."""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    return bom, "\r\n" if "\r\n" in text else "\n", text[len(bom):].replace("\r\n", "\n")


def _styled(bom: str, eol: str, text: str) -> str:
    return bom + text.replace("\n", eol)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _one_line(value) -> str:
    """Entry fields are single lines: reviewer or judgment text cannot forge a `status:` line or a `### DW-` heading."""
    return " ".join(str(value).split())


def _read_at(repo, commit: str, path: str, what: str) -> str:
    try:
        return subprocess.run(["git", *GIT_SAFE, "-C", str(repo.worktree), "show", f"{commit}:{path}"], check=True,
                              capture_output=True, stdin=subprocess.DEVNULL).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        raise _invalid(f"{what} {path} is not readable at commit {str(commit)[:12]}") from exc


@dataclass(frozen=True)
class EpicStory:
    title: str
    user_story: list[str]
    acceptance: list[str]
    preconditions: list[str]
    text: str   # the entry verbatim, its heading included (the spec author's brief)


def parse_epic_story(text: str, number: str) -> EpicStory:
    """Design D24: the `### Story <N>.<M>: <title>` entry of an epics file, up to the next heading of level 1 to 3:
    its user story lines, the bullets of a `**Preconditions:**` block and the `**Given**/**When**/**Then**/**And**`
    acceptance lines, verbatim (a leading `- ` of a user story or acceptance line dropped), and the entry's text."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    heads = list(_STORY_HEADING.finditer(text))
    index = next((i for i, m in enumerate(heads) if f"{m.group(1)}.{m.group(2)}" == number), None)
    if index is None:
        raise _invalid(f"Story {number} is not in the epics file")
    following = _SECTION_END.search(text, heads[index].end())
    end = following.start() if following else len(text)
    body = text[heads[index].end():end]
    lines = [line.strip() for line in body.split("\n")]
    unbulleted = [line.removeprefix("- ").strip() for line in lines]
    user_story = [line for line in unbulleted if line.startswith(("As a", "I want", "So that"))]
    acceptance = [line for line in unbulleted if line.startswith(("**Given**", "**When**", "**Then**", "**And**"))]
    preconditions, inside = [], False
    for line in lines:
        if line.startswith("**Preconditions:**"):
            inside = True
            continue
        if inside and line.startswith("- "):
            preconditions.append(line[2:].strip())
        elif inside and line and not line.startswith("- "):
            inside = False
    return EpicStory(heads[index].group(3).strip(), user_story, acceptance, preconditions,
                     text[heads[index].start():end].strip("\n"))


def _in_sprint_status(text: str, story_key: str) -> bool:
    """Design D24: the key is in the `development_status` map at `backlog` or `ready-for-dev`."""
    lines = _plain(text)[2].split("\n")
    start = next((index for index, line in enumerate(lines) if line.rstrip() == "development_status:"), None)
    for line in lines[start + 1:] if start is not None else []:
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            break
        match = _STATUS_LINE.match(line)
        if match is not None and match.group(2).strip() == story_key:
            return _unquote(match.group(3)) in ("backlog", READY)
    return False


def _epic_entry(repo, base_commit: str, story_key: str, epics_path: str, spec_path: str) -> EpicStory:
    """Design D24: the epics entry of a story key listed in sprint-status, whose spec is not written yet."""
    epics = _read_at(repo, base_commit, epics_path, "Epics file")
    if not _in_sprint_status(_read_at(repo, base_commit, f"{ARTIFACTS_DIR}/sprint-status.yaml", "Sprint status"),
                             story_key):
        raise _invalid(f"Story key {story_key} is not in sprint-status.yaml")
    number = re.match(r"(\d+)-(\d+)-", story_key)
    if number is None:
        raise _invalid(f"Story key {story_key} has no epic number")
    try:
        entry = parse_epic_story(epics, f"{number.group(1)}.{number.group(2)}")
    except DomainError:
        raise _invalid(f"Story key {story_key} is not in the epics file") from None
    if not entry.acceptance:
        raise _invalid(f"Story key {story_key} has no acceptance criteria in the epics file")
    if subprocess.run(["git", *GIT_SAFE, "-C", str(repo.worktree), "cat-file", "-e", f"{base_commit}:{spec_path}"],
                      capture_output=True, stdin=subprocess.DEVNULL).returncode == 0:
        raise _invalid(f"Story spec {spec_path} exists at the base commit")
    return entry


def _epic_of(story_key: str) -> str | None:
    head = story_key.split("-", 1)[0]
    return f"epic-{head}" if head.isdigit() else None


def _split_field(line: str) -> tuple[str, str]:
    """A `name: value` field line as its name and its stripped value."""
    name, _, value = line.partition(":")
    return name, value.strip()


def _settles(fields: list[str]) -> tuple[bool, str]:
    """Design epic-drive E6: `status: done …` settles; so does a `decision:` line whose option label is not empty and
    does not start with `keep-open` or `keep open`, in any case (the deferred-work format's non-settling choice). The
    label is the value less one leading `YYYY-MM-DD` date (when there is one), cut at the format's ` — <detail>`
    separator, less its leading punctuation and markup (`` `keep-open` ``, `**keep-open**`): a line that is only
    `— <detail>` has no option. A settling line beside a keep-open one is unmet (`conflicting decision: lines`,
    whatever their order). Unmet otherwise, the first decision line that does not settle gives the reason."""
    pairs = [_split_field(line) for line in fields]
    status = next((value for name, value in pairs if name == "status"), None)
    if status is not None and status.split()[:1] == ["done"]:
        return True, f"status: {status}"
    reasons, settling, kept = [], None, False
    for value in (value for name, value in pairs if name == "decision"):
        label = _LABEL_MARKUP.sub("", _DECISION_DATE.sub("", value, count=1).partition("—")[0]).lower()
        if not label:
            reasons.append("a decision: line without an option does not settle")
        elif label.startswith(("keep-open", "keep open")):
            reasons.append("a keep-open decision does not settle")
            kept = True
        elif settling is None:
            settling = value
    if settling is not None:
        return (False, "conflicting decision: lines") if kept else (True, f"decision: {settling}")
    return False, reasons[0] if reasons else "no settling decision: line and no status: done"


def _ledger_entries(text: str) -> dict[str, list[list[str]]]:
    """Design epic-drive E6: `DW-<n>` -> one list of field lines per `### DW-<n>:` block, in file order (a repeated
    heading is another block, never merged into the first). Blank lines between the heading and the first field are
    skipped (the coordinator writes one); the first line that is neither blank nor `field: value` ends the fields, as
    does a blank line after them (the deferred-work format)."""
    entries, current, started = {}, None, False
    for line in _plain(text)[2].split("\n"):
        heading = _DW.match(line)
        if heading is not None:
            current, started = [], False
            entries.setdefault(f"DW-{heading.group(1)}", []).append(current)
            continue
        if current is None:
            continue
        if not line.strip():
            if started:
                current = None
            continue
        if not line[0].isspace() and _FIELD.match(line.rstrip()):
            current.append(line.rstrip())
            started = True
        else:
            current = None
    return entries


def preconditions_state(repo, commit: str, artifacts_dir: str, bullets) -> list[dict]:
    """Design epic-drive E6: the mechanical state of each `**Preconditions:**` bullet at `commit`. A bullet names its
    entry by its first `DW-<n>`; the entry is read from `<artifacts_dir>/deferred-work.md`, else from the archive
    beside it (an archived entry keeps its lines; the archive appends, so its last block of an id counts). Met:
    `status: done` or a settling `decision:` line. Unmet with a reason otherwise: no id, no readable ledger, no entry,
    a repeated heading in the ledger, more than one `status:` line, a keep-open decision (also beside a settling one),
    nothing decided. Never raises on ledger content; `lines` are the entry's `status:`, `decision:` and `resolution:`
    lines for a brief."""
    texts = []
    for name in (_LEDGER, _ARCHIVE):
        try:
            texts.append(_read_at(repo, commit, f"{artifacts_dir}/{name}", "Deferred work"))
        except DomainError:
            texts.append(None)
    ledger, archive = (_ledger_entries(text) if text is not None else {} for text in texts)
    found = []
    for bullet in bullets:
        match = _DW_ID.search(bullet)
        item = {"bullet": bullet, "id": None, "state": "unmet", "reason": "the bullet names no DW-<n> entry",
                "lines": []}
        if match is not None:
            key = item["id"] = f"DW-{match.group(1)}"
            if texts[0] is None and texts[1] is None:
                item["reason"] = f"{artifacts_dir}/{_LEDGER} is not readable at the commit"
            elif len(ledger.get(key, [])) > 1:
                item["reason"] = f"{key} has more than one entry in the ledger"
            elif key not in ledger and key not in archive:
                item["reason"] = f"{key} has no entry in the ledger or the archive"
            else:
                fields = (ledger.get(key) or archive[key])[-1]   # the ledger first; else the archive's last block
                item["lines"] = [line for line in fields
                                 if _split_field(line)[0] in ("status", "decision", "resolution")]
                statuses = sum(_split_field(line)[0] == "status" for line in fields)
                if statuses > 1:
                    item["reason"] = f"{key} has {statuses} status: lines"
                else:
                    met, item["reason"] = _settles(fields)
                    item["state"] = "met" if met else "unmet"
        found.append(item)
    return found


def preconditions_section(found: list[dict], commit: str) -> list[str]:
    """Design epic-drive E6: the `## Preconditions` section of the spec author's and the spec reviewer's briefs."""
    if not found:
        return ["## Preconditions", "- none"]
    lines = ["## Preconditions"]
    for item in found:
        lines.append(f"- {item['bullet']}")
        lines += [f"  - `{line}`" for line in item["lines"]] or [f"  - {item['reason']}"]
    state = ", ".join(f"{item['id'] or 'unnamed'} {item['state']}" for item in found)
    lines.append(f"State at `{commit[:12]}`: {state}. A decision that does not settle its precondition, or a change it "
                 "requires in this story that the epics entry does not show, is an Open Question, never an assumption.")
    return lines


def sync_sprint_status(text: str, story_key: str, status: str) -> str:
    """Design D18: textual edit of the `development_status` map, comments and order preserved. The story's epic
    moves backlog -> in-progress when the story starts and -> done when every story of that epic is done. The map
    ends at the next top-level key; an indented line in it that is not `key: status` stops, so no epic is moved past
    a story line that was not read. A BOM and CRLF line endings are kept."""
    bom, eol, text = _plain(text)
    lines = text.split("\n")
    try:
        start = next(index for index, line in enumerate(lines) if line.rstrip() == "development_status:")
    except StopIteration:
        raise _shape("no `development_status:` map") from None
    entries = {}
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            break
        match = _STATUS_LINE.match(line)
        if match is None:
            raise _shape(f"line {index + 1} of development_status is not `key: status`: {line.strip()[:80]}")
        key = match.group(2).strip()
        if key in entries:
            raise _shape(f"key {key} appears twice in development_status (lines {entries[key][0] + 1} and {index + 1})")
        entries[key] = (index, match.group(1), match.group(3))
    if story_key not in entries:
        raise _shape(f"story key {story_key} is not in development_status")
    epic = _epic_of(story_key)
    if epic is not None and epic not in entries:
        raise _shape(f"epic key {epic} is not in development_status")
    index, indent, _ = entries[story_key]
    lines[index] = f"{indent}{story_key}: {status}"
    if epic is not None:
        prefix = story_key.split("-", 1)[0] + "-"
        states = {key: (status if key == story_key else _unquote(value[2])) for key, value in entries.items()
                  if key.startswith(prefix) and key != epic}
        eindex, eindent, current = entries[epic]
        if status == "in-progress" and _unquote(current) == "backlog":
            lines[eindex] = f"{eindent}{epic}: in-progress"
        elif status == "done" and states and all(value == "done" for value in states.values()):
            lines[eindex] = f"{eindent}{epic}: done"
    return _styled(bom, eol, "\n".join(lines))


def append_deferred_work(text: str, entries) -> str:
    """Design D19: canonical entries appended, numbered after the greatest existing DW number; a BOM and CRLF are
    kept."""
    if not entries:
        return text
    bom, eol, text = _plain(text)
    number = max((int(item) for item in _DW.findall(text)), default=0)
    blocks = []
    for entry in entries:
        number += 1
        one = {key: _one_line(value) for key, value in entry.items() if value is not None}
        lines = [f"### DW-{number}: {one['title']}", "", f"origin: {one['origin']}", f"location: {one['location']}",
                 f"source_spec: `{one['source_spec']}`", f"severity: {one['severity']}", f"reason: {one['reason']}",
                 "status: open"]
        if one.get("decision"):
            lines.append(f"decision: {one['decision']}")
        blocks.append("\n".join(lines) + "\n")
    joined = "\n".join(blocks)
    return _styled(bom, eol, (text.rstrip("\n") + "\n\n" + joined) if text else joined)


def sprint_entries(text: str) -> dict[str, str]:
    """The `development_status` map as `{key: status}`; the map ends at the next top-level key (D18's rule)."""
    lines = _plain(text)[2].split("\n")
    start = next((index for index, line in enumerate(lines) if line.rstrip() == "development_status:"), None)
    if start is None:
        raise DomainError("STALE_INPUT", "story-status: sprint-status.yaml has no development_status map")
    entries = {}
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            break
        match = _STATUS_LINE.match(line)
        if match is not None:
            entries[match.group(2).strip()] = _unquote(match.group(3))
    return entries


def plan_stories(repo, commit: str, epics_path: str, epic: int) -> list[dict]:
    """Design epic-drive E5: the `### Story <epic>.<M>:` headings of the epics file at `commit`, in order, each with
    the one `development_status` key starting `<epic>-<M>-` and its status; `key` is None (and `keys` the candidates)
    when none or several match. No story of the epic is `STALE_INPUT`."""
    epics = _read_at(repo, commit, epics_path, "Epics file")
    sprint = _read_at(repo, commit, f"{ARTIFACTS_DIR}/sprint-status.yaml",
                                     "Sprint status")
    entries = sprint_entries(sprint)
    text = epics.lstrip("﻿").replace("\r\n", "\n")
    found = []
    for match in _STORY_HEADING.finditer(text):
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
