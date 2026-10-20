"""Explicit, source-only installation of the portable coordinator package."""

import argparse
import difflib
import fcntl
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from coordinator_core.repo import resolve_repo
from coordinator_core.installation import check_package_entries


SOURCE = Path(__file__).parent
PACKAGE = ".bmad-coordinator"
BEGIN = b"<!-- bmad-coordinator:begin v1 -->"
END = b"<!-- bmad-coordinator:end v1 -->"


@dataclass(frozen=True)
class InstallPreview:
    sha256: str
    agents_diff: str
    files: list[dict[str, str]]
    warnings: list[str]


@dataclass(frozen=True)
class InstallResult:
    applied: bool
    package_revision: str


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _regular_or_absent(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError(f"Nonregular destination: {path}")
    return path.read_bytes()


def _safe_parts(root, relative):
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in (".", "..") for part in pure.parts):
        raise ValueError(f"Unsafe package path: {relative}")
    path = root
    for part in pure.parts[:-1]:
        path /= part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError(f"Non-directory package parent: {path}")
    return root / relative


def _source(relative):
    path = _safe_parts(SOURCE, relative)
    data = _regular_or_absent(path)
    if data is None:
        raise ValueError(f"Missing source: {relative}")
    return data


def _block(agents, content, installed_block_sha256=None):
    if agents.count(BEGIN) or agents.count(END):
        if agents.count(BEGIN) != 1 or agents.count(END) != 1:
            raise ValueError("Changed or duplicate AGENTS markers")
        start = agents.index(BEGIN)
        if installed_block_sha256 is not None:
            end = agents.find(b"\n", agents.index(END)) + 1 or len(agents)
            if agents[end:] or _sha(agents[start:end]) != installed_block_sha256:
                raise ValueError("Changed or misplaced managed AGENTS block")
            return agents[:start] + content, True
        if agents[start:] != content:
            raise ValueError("Changed or misplaced managed AGENTS block")
        return agents, True
    if installed_block_sha256 is not None:
        raise ValueError("Changed or misplaced managed AGENTS block")
    join = b"" if not agents or agents.endswith(b"\n") else b"\n"
    return agents + join + content, False


def _installed(repo):
    """Return the installed manifest an upgrade starts from and its digests by package path."""
    relative = f"{PACKAGE}/manifest.json"
    data = _regular_or_absent(_safe_parts(repo, relative))
    if data is None:
        raise ValueError("No installed package to upgrade")
    try:
        manifest = json.loads(data)
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or \
       set(manifest) != {"format", "package_revision", "files", "block_sha256"} or \
       type(manifest["format"]) is not int or manifest["format"] != 1 or \
       not isinstance(manifest["package_revision"], str) or not isinstance(manifest["block_sha256"], str) or \
       not isinstance(manifest["files"], dict) or "manifest.json" in manifest["files"] or \
       not all(name and PurePosixPath(name).as_posix() == name and isinstance(digest, str)
               for name, digest in manifest["files"].items()):
        raise ValueError("Installed manifest is malformed")
    old = {relative: _sha(data)}
    for name, digest in manifest["files"].items():
        _safe_parts(repo / PACKAGE, name)
        old[f"{PACKAGE}/{name}"] = digest
    return manifest, old


def _build(repo, upgrade=False, installed_block_sha256=None):
    repo = Path(repo).absolute()
    if resolve_repo(repo).worktree != repo.resolve():
        raise ValueError("--repo must name the Git worktree root")
    manifest_source = _source("package/manifest.json")
    source_manifest = json.loads(manifest_source)
    if source_manifest.get("format") != 1 or not isinstance(source_manifest.get("files"), list) or \
       not isinstance(source_manifest.get("block_source"), str):
        raise ValueError("Unsupported source manifest")
    sources = {}
    for entry in source_manifest["files"]:
        if set(entry) != {"source", "destination"} or not all(isinstance(v, str) for v in entry.values()):
            raise ValueError("Invalid source manifest entry")
        destination = entry["destination"]
        if destination in sources or destination == "manifest.json":
            raise ValueError("Duplicate package destination")
        sources[destination] = (entry["source"], _source(entry["source"]))
    if "run.py" not in sources or "coordinator_core/installation.py" not in sources:
        raise ValueError("Incomplete package allowlist")
    block = _source(source_manifest["block_source"])
    if block.count(BEGIN) != 1 or block.count(END) != 1 or \
       not block.startswith(BEGIN) or not block.rstrip().endswith(END) or not block.endswith(b"\n"):
        raise ValueError("Invalid managed instruction block")
    revision = _sha(_json({"source_manifest": _sha(manifest_source),
                           "files": {name: _sha(data) for name, (_, data) in sources.items()},
                           "block": _sha(block)}))
    installed_manifest = _json({"format": 1, "package_revision": revision,
                                "files": {name: _sha(data) for name, (_, data) in sources.items()},
                                "block_sha256": _sha(block)})
    installed, old = _installed(repo) if upgrade else (None, {})
    if upgrade and installed_block_sha256 is None:
        installed_block_sha256 = installed["block_sha256"]
    agents_path = _safe_parts(repo, "AGENTS.md")
    agents = _regular_or_absent(agents_path)
    agents_present = agents is not None
    agents = b"" if agents is None else agents
    proposed, active = _block(agents, block, installed_block_sha256)
    targets = {f"{PACKAGE}/{name}": data for name, (_, data) in sources.items()}
    targets[f"{PACKAGE}/manifest.json"] = installed_manifest
    removed = [relative for relative in old if relative not in targets]
    check_package_entries(repo / PACKAGE, set(sources) | {"manifest.json"} |
                          {relative.removeprefix(f"{PACKAGE}/") for relative in old})
    for relative, digest in old.items():
        current = _regular_or_absent(_safe_parts(repo, relative))
        if current is None:
            raise ValueError("Activated package has missing files; reviewed repair required")
        if _sha(current) != digest:
            raise ValueError(f"Package collision: {relative}")
    observed = {}
    for relative, data in targets.items():
        path = _safe_parts(repo, relative)
        current = _regular_or_absent(path)
        if current is not None and current != data and relative not in old:
            raise ValueError(f"Package collision: {relative}")
        observed[relative] = None if current is None else _sha(current)
    if active and not upgrade and any(value is None for value in observed.values()):
        raise ValueError("Activated package has missing files; reviewed repair required")
    lines = difflib.unified_diff(agents.decode("utf-8", "replace").splitlines(keepends=True),
                                 proposed.decode("utf-8", "replace").splitlines(keepends=True),
                                 fromfile="AGENTS.md", tofile="AGENTS.md (proposed)")
    warnings = []
    for relative in targets:
        check = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", relative],
                               capture_output=True)
        if check.returncode == 0:
            warnings.append(f"Ignored destination: {relative}")
        elif check.returncode != 1:
            raise ValueError("Cannot inspect Git ignore rules")
    state = {"revision": revision, "agents": _sha(agents) if agents_present else None,
             "targets": observed,
             "warnings": warnings}
    if upgrade:
        state |= {"upgrade": True, "installed_revision": installed["package_revision"]}
    token = _sha(_json(state))
    files = []
    for relative, data in targets.items():
        name = relative.removeprefix(f"{PACKAGE}/")
        status = "absent" if observed[relative] is None else "exact" if observed[relative] == _sha(data) else "upgrade"
        files.append({"source": sources[name][0] if name != "manifest.json" else "generated",
                      "destination": relative, "sha256": _sha(data), "status": status})
    for relative in removed:
        files.append({"source": "installed", "destination": relative, "sha256": old[relative], "status": "remove"})
    return InstallPreview(token, "".join(lines), files, warnings), targets, agents, proposed, revision, \
        agents_present, installed, old


def _replace(repo, relative, data, digest):
    """Atomically replace a package file whose bytes must still have the installed digest."""
    path = _safe_parts(repo, relative)
    temporary = Path(f"{path}.bmad-upgrade.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        current = _regular_or_absent(path)
        if current is None or _sha(current) != digest:
            raise ValueError(f"Package changed during apply: {relative}")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def preview(repo: Path, upgrade: bool = False) -> InstallPreview:
    return _build(repo, upgrade)[0]


def apply(repo: Path, expected_sha256: str, upgrade: bool = False) -> InstallResult:
    plan, targets, original_agents, proposed_agents, revision, agents_present, installed, old = _build(repo, upgrade)
    if expected_sha256 != plan.sha256:
        raise ValueError("Preview is stale; run --preview again")
    if plan.warnings:
        raise ValueError("Ignored package destination; installation refused")
    if original_agents == proposed_agents and all(item["status"] == "exact" for item in plan.files):
        return InstallResult(False, revision)
    repo = Path(repo).absolute()
    status = {item["destination"]: item["status"] for item in plan.files}
    manifest = f"{PACKAGE}/manifest.json"
    for relative, data in targets.items():
        if upgrade and relative == manifest:
            continue
        if status[relative] == "upgrade":
            _replace(repo, relative, data, old[relative])
            continue
        path = _safe_parts(repo, relative)
        if path.exists():
            if _regular_or_absent(path) != data:
                raise ValueError(f"Package changed during apply: {relative}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path = _safe_parts(repo, relative)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    if upgrade:
        for relative, value in status.items():
            if value == "remove":
                path = _safe_parts(repo, relative)
                current = _regular_or_absent(path)
                if current is None or _sha(current) != old[relative]:
                    raise ValueError(f"Package changed during apply: {relative}")
                path.unlink()
        _replace(repo, manifest, targets[manifest], old[manifest])
    staged, _, current_agents, _, current_revision, current_agents_present, *_ = _build(
        repo, upgrade, installed and installed["block_sha256"])
    if current_agents != original_agents or current_agents_present != agents_present or \
       current_revision != revision or staged.warnings or any(item["status"] != "exact" for item in staged.files):
        raise ValueError("Package or AGENTS.md changed during staging")
    agents_path = _safe_parts(repo, "AGENTS.md")
    if not agents_present:
        fd = os.open(agents_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    else:
        fd = os.open(agents_path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("AGENTS.md became nonregular")
        current = stream.read()
        if current != original_agents or agents_path.stat().st_ino != os.fstat(stream.fileno()).st_ino:
            raise ValueError("AGENTS.md changed during apply")
        if upgrade:
            stream.seek(0)
            stream.write(proposed_agents)
            stream.truncate()
        else:
            stream.seek(0, os.SEEK_END)
            stream.write(proposed_agents[len(original_agents):])
        stream.flush()
        os.fsync(stream.fileno())
    return InstallResult(True, revision)


def main():
    parser = argparse.ArgumentParser(description="Preview or explicitly install the portable coordinator")
    parser.add_argument("--repo", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--preview-sha256")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    try:
        if args.preview:
            result = preview(args.repo, args.upgrade)
        else:
            if not args.preview_sha256:
                parser.error("--apply requires --preview-sha256")
            result = apply(args.repo, args.preview_sha256, args.upgrade)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"install: {exc}\n")
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
