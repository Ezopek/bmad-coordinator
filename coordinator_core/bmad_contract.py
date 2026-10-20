"""Read-only discovery of the installed BMAD PRD/architecture/review contract and its semantic fingerprint.

No hashes of BMAD releases are pinned: capabilities are recognized from installed bytes, and whether they are
semantically compatible is a coordinator judgment recorded as a decision, never proven here."""

import csv
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model import DomainError
from .run_receipt import canonical

REQUIRED = ("bmad-prd", "bmad-architecture", "bmad-review")
MAX_REFERENCED = 200
SIDE_EFFECT_KEYS = ("external_handoffs", "on_complete", "external_sources")
PLACEHOLDER = re.compile(r"\{(?!project-root\})[^{}]*\}")
# (skill, file under the skill, substrings the installed text must contain) per recognized capability.
MARKERS = {
    "prd_headless": ("bmad-prd", "references/headless.md", ("headless: true", "doc_workspace")),
    "prd_status_enum": ("bmad-prd", "assets/headless-schemas.md", ('"complete"', '"partial"', '"blocked"')),
    "prd_artifact_keys": ("bmad-prd", "assets/headless-schemas.md", ('"prd"', '"memlog"')),
    "prd_question_fields": ("bmad-prd", "assets/headless-schemas.md",
                            ("open_questions", "assumptions", "conflicts_with_prior_decisions")),
    "architecture_headless": ("bmad-architecture", "references/headless.md", ("headless: true", "doc_workspace")),
    "architecture_status_enum": ("bmad-architecture", "references/headless.md", ("complete | partial | blocked",)),
    "architecture_artifact_keys": ("bmad-architecture", "references/headless.md", ('"spine"', '"memlog"')),
    "architecture_question_fields": ("bmad-architecture", "references/headless.md",
                                     ("open_questions", "assumptions", "conflicts_with_prior_decisions",
                                      "altitude", "purpose")),
}
LIMITATIONS = [
    "semantic compatibility is not judged; it needs a recorded coordinator decision for the fingerprint",
    "capability recognition proves the shape of installed text, not that a worker will follow it",
    "network research, MCP tools and URLs used by skills are not fingerprinted",
    "worker permission, descendant and reading statements stay claims",
]


@dataclass(frozen=True)
class ContractReport:
    supported: bool
    capabilities: dict[str, bool]
    limitations: list[str]
    reasons: list[str]
    fingerprint_sha256: str | None
    contract_files: list[dict[str, str]]
    contract: dict
    facts: dict


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(contract: dict) -> str:
    return _sha(canonical(contract))


def _version(exe: str) -> str:
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _resolve(argv: list[str], worktree: Path):
    """Run an installed resolver under this interpreter; (parsed JSON object, sha256 of stdout) or (None, '')."""
    try:
        done = subprocess.run([sys.executable, "-B", *argv], cwd=worktree, capture_output=True, timeout=30,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        value = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, ""
    return (value, _sha(done.stdout)) if isinstance(value, dict) else (None, "")


def _strings(value, key=""):
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _strings(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)
    elif isinstance(value, str):
        yield key, value


def _plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and os.path.realpath(path) == str(path)


def _skill_name(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0] != "---":
        return None
    for line in lines[1:]:
        if line == "---":
            return None
        if line.startswith("name:"):
            return line[5:].strip().strip("\"'")
    return None


def _catalog(worktree: Path, reasons: list[str]) -> list[str]:
    """Required IDs must be catalogued; returns the forwarding shim IDs. Catalog path columns are never opened."""
    path = worktree / "_bmad/_config/skill-manifest.csv"
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error):
        reasons.append("skill catalog _bmad/_config/skill-manifest.csv is unreadable")
        return []
    ids = {row.get("canonicalId") for row in rows}
    reasons += [f"required skill missing from catalog: {item}" for item in REQUIRED if item not in ids]
    shims = []
    for row in rows:
        description = row.get("description") or ""
        if description.startswith("Deprecated") and "forwards to" in description:
            if description.split("forwards to", 1)[1].strip() in REQUIRED and row.get("canonicalId"):
                shims.append(row["canonicalId"])
    return sorted(shims)


def _manifest_check(worktree: Path, client_root: str, skills: list[str]) -> str:
    """matched | locally-modified | unmapped, comparing installed bytes with files-manifest.csv hashes (a fact)."""
    try:
        with (worktree / "_bmad/_config/files-manifest.csv").open(newline="", encoding="utf-8") as stream:
            expected = {row["path"]: row["hash"] for row in csv.DictReader(stream)}
    except (OSError, UnicodeError, csv.Error, KeyError):
        return "unmapped"
    modified = unmapped = 0
    for skill in skills:
        for path in sorted((worktree / client_root / skill).rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            rel = path.relative_to(worktree / client_root / skill).as_posix()
            known = next((hash_ for name, hash_ in expected.items() if name.endswith(f"/{skill}/{rel}")), None)
            if known is None:
                unmapped += 1
            elif known != _sha(path.read_bytes()):
                modified += 1
    return "locally-modified" if modified else "unmapped" if unmapped else "matched"


def _tree_digest(path: Path) -> str:
    entries = [(item.relative_to(path).as_posix(), _sha(item.read_bytes()))
               for item in sorted(path.rglob("*")) if item.is_file() and "__pycache__" not in item.parts]
    return _sha(canonical(entries))


def discover(worktree: Path, client_root: str = ".agents/skills") -> ContractReport:
    worktree = Path(worktree).resolve()
    posix = PurePosixPath(client_root)
    if not client_root or posix.is_absolute() or ".." in posix.parts or str(posix) != client_root:
        raise DomainError("INVALID_INPUT", "Client root must be a normalized relative path")
    bmad, base = worktree / "_bmad", worktree / client_root
    reasons: list[str] = []
    files: dict[str, str] = {}
    references: list[dict] = []

    def add(path: Path) -> None:
        rel = path.relative_to(worktree).as_posix()
        try:
            files[rel] = _sha(path.read_bytes())
        except OSError:
            reasons.append(f"cannot read contract file: {rel}")

    def tree(directory: Path) -> None:
        for here, dirs, names in os.walk(directory):
            for name in list(dirs):
                if name == "__pycache__":
                    dirs.remove(name)
                elif (Path(here) / name).is_symlink():
                    dirs.remove(name)
                    reasons.append(f"symlink in contract: {(Path(here) / name).relative_to(worktree).as_posix()}")
            dirs.sort()
            for name in sorted(names):
                item = Path(here) / name
                if item.is_symlink() or not item.is_file():
                    reasons.append(f"symlink or special file in contract: {item.relative_to(worktree).as_posix()}")
                else:
                    add(item)

    # 1. Catalog and installed skill directories.
    shims = _catalog(worktree, reasons)
    skills = []
    for skill in REQUIRED:
        directory = base / skill
        if directory.is_symlink() or not directory.is_dir():
            reasons.append(f"skill is missing or not a real directory: {client_root}/{skill}")
        elif _skill_name(directory / "SKILL.md") != skill:
            reasons.append(f"skill SKILL.md is missing or its name differs from the directory: {skill}")
        elif not (directory / "customize.toml").is_file():
            reasons.append(f"skill customize.toml is missing: {skill}")
        else:
            skills.append(skill)
            tree(directory)

    # 2. Capability recognition from installed text only.
    capabilities = {}
    for name, (skill, rel, markers) in MARKERS.items():
        try:
            text = (base / skill / rel).read_text(encoding="utf-8") if skill in skills else ""
        except (OSError, UnicodeError):
            text = ""
        capabilities[name] = bool(text) and all(marker in text for marker in markers)
        if skill in skills and not capabilities[name]:
            reasons.append(f"capability marker missing for {skill}: {name} ({rel})")
    capabilities["memlog_append"] = all(
        "memlog.py" in ((base / skill / "references/headless.md").read_text(encoding="utf-8")
                        if (base / skill / "references/headless.md").is_file() else "")
        for skill in ("bmad-prd", "bmad-architecture"))
    scripts = {name: (bmad / "scripts" / name).is_file()
               for name in ("resolve_customization.py", "resolve_config.py", "memlog.py")}
    capabilities["resolver"] = scripts["resolve_customization.py"] and scripts["resolve_config.py"]
    capabilities["memlog_script"] = scripts["memlog.py"]
    reasons += [f"installed script is missing: _bmad/scripts/{name}" for name, found in scripts.items() if not found]
    if scripts["memlog.py"] and not _run_ok([str(bmad / "scripts/memlog.py"), "--help"], worktree):
        capabilities["memlog_script"] = False
        reasons.append("installed memlog.py does not run under this interpreter")
    # 3. Contract closure: scripts, config, custom layer.
    if (bmad / "scripts").is_dir() and not (bmad / "scripts").is_symlink():
        tree(bmad / "scripts")
    for name in ("manifest.yaml", "skill-manifest.csv", "bmad-help.csv"):
        if (bmad / "_config" / name).is_file():
            add(bmad / "_config" / name)
    for item in sorted(bmad.glob("config*")) + sorted(bmad.glob("*/config*")):
        if item.is_symlink():
            reasons.append(f"symlink in contract: {item.relative_to(worktree).as_posix()}")
        elif item.is_file():
            add(item)
    custom, listing = bmad / "custom", []
    if custom.is_symlink() or (custom.exists() and not custom.is_dir()):
        reasons.append("_bmad/custom is not a real directory")
    elif custom.is_dir():
        for item in sorted(custom.iterdir()):
            listing.append(item.name)
            if item.is_symlink() or item.is_dir():
                reasons.append(f"unsupported symlink or subdirectory in _bmad/custom: {item.name}")
            else:
                add(item)
    # 4. Explicit absence of optional layers, so an added override changes the fingerprint.
    optional = ["_bmad/config.user.toml", "_bmad/config.user.yaml", "_bmad/custom/config.toml",
                "_bmad/custom/config.user.toml"] + [f"_bmad/custom/{skill}{suffix}" for skill in sorted({*REQUIRED, *shims})
                                                     for suffix in (".toml", ".user.toml")]
    absent = sorted(item for item in optional if not os.path.lexists(worktree / item))
    # 5. Coordinator-side resolution; 6. side-effect gates and reachable references.
    resolved = {"central": "", "skills": {}}
    central, resolved["central"] = _resolve([str(bmad / "scripts/resolve_config.py"), "--project-root", str(worktree)],
                                            worktree) if scripts["resolve_config.py"] else (None, "")
    if central is None:
        reasons.append("central config resolver failed or produced unparseable output")
    lenses = set()
    for skill in skills:
        if not scripts["resolve_customization.py"]:
            break
        value, digest = _resolve([str(bmad / "scripts/resolve_customization.py"), "--skill", str(base / skill),
                                  "--project-root", str(worktree), "--key", "workflow"], worktree)
        workflow = value.get("workflow") if value else None
        if not isinstance(workflow, dict):
            reasons.append(f"customization resolver failed or has no workflow key: {skill}")
            continue
        resolved["skills"][skill] = digest
        lenses |= {item.get("code") for item in workflow.get("lenses", []) if isinstance(item, dict)} \
            if skill == "bmad-review" else set()
        for key, text in _strings(workflow):
            if text.startswith("skill:") and text[6:].split()[:1] != ["bmad-review"]:
                reasons.append(f"{skill}: foreign skill reference is not supported: {key}")
        reasons += [f"{skill}: side-effect customization is not supported: {key}"
                    for key, item in workflow.items()
                    if (key in SIDE_EFFECT_KEYS or key.startswith("activation_steps")) and item not in ("", [], None)]
        for key, text in _strings(workflow):
            ref = text[5:].strip() if text.startswith("file:") else text if key.endswith("_template") and text else None
            if ref is None:
                continue
            if PLACEHOLDER.search(ref):
                reasons.append(f"{skill}: unresolved placeholder in {key} reference")
                continue
            expanded = ref.replace("{project-root}", str(worktree))
            full = os.path.normpath(expanded if os.path.isabs(expanded) else base / skill / expanded)
            if not Path(full).is_relative_to(worktree):
                reasons.append(f"{skill}: reference points outside the worktree: {key}")
                continue
            if any(char in full for char in "*?["):
                found = [Path(item) for item in itertools.islice(glob.iglob(full, recursive=True), MAX_REFERENCED + 1)
                         if Path(item).is_file()]
            else:
                found = [Path(full)]
                if not _plain_file(found[0]):
                    reasons.append(f"{skill}: referenced file is missing or not a plain file: "
                                   f"{Path(full).relative_to(worktree).as_posix()}")
                    continue
            if len(found) > MAX_REFERENCED:
                reasons.append(f"{skill}: more than {MAX_REFERENCED} referenced files for {key}")
                continue
            if any(not _plain_file(item) for item in found):
                reasons.append(f"{skill}: referenced file is a symlink or special file for {key}")
                continue
            entry = {"skill": skill, "ref": ref, "paths": sorted(item.relative_to(worktree).as_posix() for item in found)}
            if entry not in references:
                references.append(entry)
            for item in found:
                add(item)
    if skills == list(REQUIRED) and not {"adversarial", "edge-case-hunter"} <= lenses:
        reasons.append("bmad-review does not offer the adversarial and edge-case-hunter lenses")
    capabilities["review_lenses"] = {"adversarial", "edge-case-hunter"} <= lenses
    if len({path for item in references for path in item["paths"]}) > MAX_REFERENCED:
        reasons.append(f"more than {MAX_REFERENCED} files are reachable from merged customization")
    # 7. Tools.
    uv = shutil.which("uv")
    capabilities["uv"] = bool(uv)
    if not uv:
        reasons.append("uv is not on PATH")
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True)
    sibling = {skill: ("absent" if not (worktree / ".claude/skills" / skill).is_dir() else
                       "same" if _tree_digest(worktree / ".claude/skills" / skill) == _tree_digest(base / skill)
                       else "differs") for skill in skills} if client_root != ".claude/skills" else {}
    facts = {"head_commit": head.stdout.strip() if head.returncode == 0 else "",
             "tools": {"uv": _version(uv) if uv else "", "python": sys.version.split()[0]},
             "sibling_parity": sibling, "manifest_check": _manifest_check(worktree, client_root, skills)}
    contract_files = [{"path": path, "digest": digest} for path, digest in sorted(files.items())]
    body = {"format": 1, "client_root": client_root, "files": contract_files,
            "references": sorted(references, key=lambda item: (item["skill"], item["ref"])),
            "absent": absent, "custom_listing": listing, "resolved": resolved}
    reasons = list(dict.fromkeys(reasons))
    return ContractReport(not reasons, capabilities, list(LIMITATIONS), reasons,
                          None if reasons else fingerprint(body), contract_files, body, facts)


def _run_ok(argv: list[str], worktree: Path) -> bool:
    try:
        return subprocess.run([sys.executable, "-B", *argv], cwd=worktree, capture_output=True, timeout=30,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _changes(old: dict, new: dict) -> list[str]:
    before = {item["path"]: item["digest"] for item in old.get("files", [])}
    after = {item["path"]: item["digest"] for item in new.get("files", [])}
    found = [f"changed: {path}" for path in sorted(before.keys() & after.keys()) if before[path] != after[path]]
    found += [f"added: {path}" for path in sorted(after.keys() - before.keys())]
    found += [f"removed: {path}" for path in sorted(before.keys() - after.keys())]
    return found + [f"changed: {key}" for key in ("references", "absent", "custom_listing", "resolved", "client_root")
                    if old.get(key) != new.get(key)]


def verify_fingerprint(worktree: Path, expected_sha256: str, client_root: str | None = None,
                       contract: dict | None = None) -> list[str]:
    """[] when the live contract equals the expected fingerprint; otherwise named changes (or reasons)."""
    report = discover(worktree, client_root or (contract or {}).get("client_root", ".agents/skills"))
    if not report.supported:
        return report.reasons
    if report.fingerprint_sha256 == expected_sha256:
        return []
    return _changes(contract, report.contract) if contract else ["contract fingerprint differs"]


def compat_decision(snapshot: dict, feature_id: str, mandate_revision: int, fingerprint_sha256: str) -> str | None:
    """ID of the coordinator decision that judged this fingerprint at the current mandate revision."""
    for row in snapshot["decisions"]:
        if (row["feature_id"], row["mandate_revision"], row["source_kind"], row["source_ref"]) == \
           (feature_id, mandate_revision, "coordinator", f"bmad-fingerprint:{fingerprint_sha256}") and \
           json.loads(row["choice_json"]) == {"bmad_compat": fingerprint_sha256}:
            return row["id"]
    return None


def check_binding(worktree: Path | None, binding: dict, store, feature: dict) -> list[str]:
    """Shared gate for every bound BMAD stage: live fingerprint (drift is STALE_INPUT), then compat decision."""
    if worktree is None:
        return ["feature worktree is unresolved"]
    report = discover(worktree, binding["client_root"])
    if not report.supported:
        return report.reasons
    if report.fingerprint_sha256 != binding["fingerprint_sha256"]:
        raise DomainError("STALE_INPUT", "BMAD contract changed since binding: "
                          + "; ".join(_changes(binding["contract"], report.contract)[:10]))
    if compat_decision(store.read_snapshot(), feature["id"], feature["mandate_revision"],
                       binding["fingerprint_sha256"]) is None:
        return ["no coordinator compatibility decision for this fingerprint at the current mandate revision"]
    return []


def central_path(worktree: Path, key: str) -> str:
    """Worktree-relative path from a merged central config key; unresolved placeholders fail, never default."""
    worktree = Path(worktree).resolve()
    script = worktree / "_bmad/scripts/resolve_config.py"
    value, _ = _resolve([str(script), "--project-root", str(worktree)], worktree) if script.is_file() else (None, "")
    if value is None:
        raise DomainError("UNSUPPORTED_CONTRACT", "Central config cannot be resolved")
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value or PLACEHOLDER.search(value):
        raise DomainError("UNSUPPORTED_CONTRACT", f"Central config key {key} is absent or has unresolved placeholders")
    path = Path(os.path.normpath(value.replace("{project-root}", str(worktree))))
    if not path.is_absolute() or not path.is_relative_to(worktree) or path == worktree:
        raise DomainError("UNSUPPORTED_CONTRACT", f"Central config key {key} must resolve inside the worktree")
    return path.relative_to(worktree).as_posix()
