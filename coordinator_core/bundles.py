"""Capture and compare byte-bound review bundles."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .model import DomainError
from .repo import GIT_SAFE
from .service import _files, _json, _safe_feature_paths


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_bytes(manifest: dict) -> bytes:
    return _json(manifest).encode()


def capture(worktree: Path, bundle_root: Path, feature_id: str, mandate_revision: int,
            artifact_paths: list[str], dependency_paths: list[str], contracts: list[dict],
            assignment_ids: list[str], author_ids: list[str],
            decisions: list[dict] | None = None, commit: dict | None = None) -> tuple[str, Path, dict]:
    if not artifact_paths or not isinstance(artifact_paths, list) or not isinstance(dependency_paths, list):
        raise DomainError("INVALID_INPUT", "Bundle requires artifact paths")
    if contracts:   # implementation drive D22: a story bundle seals no contract files
        _files(worktree, contracts, check_current=True)
    all_paths = artifact_paths + dependency_paths + [entry["path"] for entry in contracts]
    if len(all_paths) != len(set(all_paths)):
        raise DomainError("INVALID_INPUT", "Bundle paths must be distinct")
    _safe_feature_paths(worktree, all_paths)
    files = []
    for kind, paths in (("artifact", artifact_paths), ("dependency", dependency_paths)):
        for path in paths:
            try:
                files.append({"kind": kind, "path": path, "digest": _digest(worktree / path)})
            except OSError as exc:
                raise DomainError("UNKNOWN_EVIDENCE", "Bundle source is unavailable") from exc
    files.extend({"kind": "contract", **entry} for entry in contracts)
    manifest = {"feature_id": feature_id, "mandate_revision": mandate_revision,
                "files": sorted(files, key=lambda item: (item["kind"], item["path"])),
                "source_assignment_ids": sorted(assignment_ids),
                "derived_author_ids": sorted(author_ids)}
    if decisions:  # the contributors' resolving decisions, digest-bound; absent otherwise, so other ids stay the same
        manifest["decisions"] = sorted(decisions, key=lambda item: item["id"])
    if commit:  # a commit-bound bundle (implementation drive D13); absent for planning bundles, so their ids stay
        manifest["commit"] = {key: commit[key] for key in sorted(commit)}
    data = _manifest_bytes(manifest)
    bundle_id = hashlib.sha256(data).hexdigest()
    path = bundle_root / bundle_id
    if path.exists():
        if not sealed_copy(path, manifest):
            raise DomainError("UNKNOWN_EVIDENCE", "Published bundle cannot be verified")
    else:
        bundle_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=bundle_root))
        try:
            for entry in manifest["files"]:
                copy_file(worktree / entry["path"], staging / entry["kind"] / entry["path"])
            (staging / "manifest.json").write_bytes(data)
            if source_state(worktree, manifest) != "current" or not compare_copy(staging, manifest):
                raise DomainError("UNKNOWN_EVIDENCE", "Bundle source changed during capture")
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                if not sealed_copy(path, manifest):
                    raise DomainError("UNKNOWN_EVIDENCE", "Concurrent bundle publish is not sealed")
            else:
                for item in staging.iterdir():
                    item.rename(path / item.name)
                if source_state(worktree, manifest) != "current" or not compare_copy(path, manifest):
                    raise DomainError("UNKNOWN_EVIDENCE", "Bundle source changed during publish")
                for copied in path.rglob("*"):
                    copied.chmod(0o444 if copied.is_file() else 0o555)
                path.chmod(0o555)
        finally:
            shutil.rmtree(staging)
    if source_state(worktree, manifest) != "current" or not sealed_copy(path, manifest):
        raise DomainError("UNKNOWN_EVIDENCE", "Bundle source changed after publish")
    return bundle_id, path, manifest


def read_manifest(path: Path, expected_digest: str) -> dict:
    try:
        data = (path / "manifest.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise ValueError("Manifest digest mismatch")
        manifest = json.loads(data)
        if _manifest_bytes(manifest) != data:
            raise ValueError("Manifest is not canonical")
        return manifest
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", "Bundle manifest cannot be verified") from exc


def compare_copy(path: Path, manifest: dict) -> bool:
    try:
        if (path / "manifest.json").read_bytes() != _manifest_bytes(manifest):
            return False
        return all(_digest(path / entry["kind"] / entry["path"]) == entry["digest"]
                   for entry in manifest["files"])
    except (OSError, KeyError, TypeError):
        return False


def sealed_copy(path: Path, manifest: dict) -> bool:
    try:
        if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o222:
            return False
        if any(item.is_symlink() or item.stat().st_mode & 0o222 for item in path.rglob("*")):
            return False
        return compare_copy(path, manifest)
    except OSError:
        return False


def source_state(worktree: Path, manifest: dict) -> str:
    try:
        for entry in manifest["files"]:
            _safe_feature_paths(worktree, [entry["path"]])
            if _digest(worktree / entry["path"]) != entry["digest"]:
                return "stale"
        return "current"
    except (OSError, DomainError, KeyError, TypeError):
        return "unknown"


# Copilot reviewer design V2: a read-mode launch's staged input copy -------------------------------------------------

INSTRUCTION_NAMES = frozenset({"agents.md", "claude.md", "gemini.md", "copilot-instructions.md"})


def read_alias(kind: str, index: int, path: str) -> str:
    """The inert alias `inputs/<kind>/<NNNN>-<name>`: the basename without a leading dot, and an instruction file's
    name replaced, so no staged path is a location or a file name a client discovers and loads."""
    name = Path(path).name.lstrip(".") or "file"
    return f"inputs/{kind}/{index:04d}-{'file.md' if name.lower() in INSTRUCTION_NAMES else name}"


def input_bytes(worktree: Path, source: dict) -> bytes:
    """One input's bytes: a regular file never followed through a link (`file`), or a regular blob at a commit
    (`commit`, `path`); anything else refuses the launch."""
    try:
        if "file" in source:
            fd = os.open(source["file"], os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("not a regular file")
                return stream.read()
        git = ["git", *GIT_SAFE, "-C", str(worktree)]
        listed = subprocess.run([*git, "ls-tree", "-z", source["commit"], "--", source["path"]], capture_output=True,
                                timeout=60, stdin=subprocess.DEVNULL, check=True).stdout.decode().rstrip("\0")
        mode, kind, blob = listed.split("\t", 1)[0].split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise OSError("not a regular file at the commit")
        return subprocess.run([*git, "cat-file", "blob", blob], capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL, check=True).stdout
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", f"Read-mode input {source.get('path') or source.get('file')} cannot be "
                          f"read: {exc}") from exc


def stage_inputs(worktree: Path, cwd: Path, inputs: list[dict]) -> list[str]:
    """Each input read once, checked against its bound digest, and that same buffer written read-only under its
    alias; a mismatch refuses the launch (UNKNOWN_EVIDENCE). The staged aliases, sorted."""
    for item in inputs:
        data = input_bytes(worktree, item["source"])
        if hashlib.sha256(data).hexdigest() != item["digest"]:
            raise DomainError("UNKNOWN_EVIDENCE", f"Read-mode input {item['path']} differs from its bound digest")
        target = cwd / item["alias"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as stream:
            stream.write(data)
        target.chmod(0o444)
    return sorted(item["alias"] for item in inputs)
