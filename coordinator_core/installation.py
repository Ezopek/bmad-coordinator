"""Read-only verification of an installed coordinator against a feature base."""

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .repo import RepoIdentity


PACKAGE = ".bmad-coordinator"
BEGIN = b"<!-- bmad-coordinator:begin v1 -->"
END = b"<!-- bmad-coordinator:end v1 -->"


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    package_revision: str
    base_commit: str
    reasons: list[str]


def check_package_entries(root: Path, files: set[str]) -> None:
    """Reject files outside the pinned package, including import-shadowing modules."""
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        raise ValueError("Package root is not a directory")
    directories = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if relative in directories and stat.S_ISDIR(mode):
            continue
        if relative in files and stat.S_ISREG(mode):
            continue
        raise ValueError(f"Unlisted or nonregular package entry: {relative}")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo.worktree), *args], check=True,
                          capture_output=True).stdout


def _safe_file(root: Path, name: str):
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("Installed parent is not a directory")
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in (".", "..") for part in pure.parts):
        raise ValueError("Unsafe installed path")
    path = root
    for part in pure.parts[:-1]:
        path /= part
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("Package parent is not a directory")
    path = root / name
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Installed file is not regular")
    return path.read_bytes()


def _block(data, digest):
    if data.count(BEGIN) != 1 or data.count(END) != 1:
        return False
    block = data[data.index(BEGIN):]
    return block.endswith(END + b"\n") and hashlib.sha256(block).hexdigest() == digest


def doctor(repo: RepoIdentity, base_ref: str) -> DoctorReport:
    reasons = []
    revision = ""
    base = ""
    try:
        base = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}").decode().strip()
        head = _git(repo, "rev-parse", "HEAD").decode().strip()
        if subprocess.run(["git", "-C", str(repo.worktree), "merge-base", "--is-ancestor", base, head],
                          capture_output=True).returncode != 0:
            reasons.append("Base commit is not an ancestor of actual HEAD")
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        reasons.append("Cannot resolve base commit or actual HEAD")
    try:
        package = repo.worktree / PACKAGE
        data = _safe_file(package, "manifest.json")
        manifest = json.loads(data)
        if set(manifest) != {"format", "package_revision", "files", "block_sha256"} or \
           manifest["format"] != 1 or not isinstance(manifest["files"], dict) or \
           not isinstance(manifest["package_revision"], str) or \
           not isinstance(manifest["block_sha256"], str):
            raise ValueError("Malformed installed manifest")
        if not {"run.py", "coordinator_core/__main__.py", "coordinator_core/installation.py"} <= set(manifest["files"]):
            raise ValueError("Installed manifest omits required runtime files")
        check_package_entries(package, set(manifest["files"]) | {"manifest.json"})
        revision = manifest["package_revision"]
        current = {f"{PACKAGE}/manifest.json": data}
        for name, digest in manifest["files"].items():
            if not isinstance(name, str) or name == "manifest.json" or not isinstance(digest, str):
                raise ValueError("Malformed installed file entry")
            content = _safe_file(package, name)
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"Installed file hash differs: {name}")
            current[f"{PACKAGE}/{name}"] = content
        agents = _safe_file(repo.worktree, "AGENTS.md")
        if not _block(agents, manifest["block_sha256"]):
            raise ValueError("Installed AGENTS block differs")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        reasons.append(f"Current package invalid: {exc}")
        return DoctorReport(False, revision, base, reasons)
    if base:
        try:
            committed_paths = set(_git(repo, "ls-tree", "-r", "-z", "--name-only", base, "--", PACKAGE).split(b"\0")) - {b""}
            expected_paths = {name.encode() for name in current}
            if committed_paths != expected_paths:
                reasons.append("Base package entries differ from installed allowlist")
        except (OSError, subprocess.CalledProcessError):
            reasons.append("Cannot inspect base package entries")
        for name, content in current.items():
            try:
                tree = _git(repo, "ls-tree", base, "--", name).decode().strip().split()
                if len(tree) < 4 or tree[0] != "100644" and tree[0] != "100755":
                    raise ValueError("not a committed regular file")
                committed = _git(repo, "show", f"{base}:{name}")
                if committed != content:
                    raise ValueError("bytes differ")
            except (OSError, subprocess.CalledProcessError, ValueError, UnicodeError) as exc:
                reasons.append(f"Base package differs at {name}: {exc}")
        try:
            base_agents = _git(repo, "show", f"{base}:AGENTS.md")
            if not _block(base_agents, manifest["block_sha256"]):
                reasons.append("Base AGENTS block differs")
        except (OSError, subprocess.CalledProcessError):
            reasons.append("Base AGENTS.md is missing")
    return DoctorReport(not reasons, revision, base, reasons)
