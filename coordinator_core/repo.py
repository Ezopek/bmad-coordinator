import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import DomainError


@dataclass(frozen=True)
class RepoIdentity:
    common_dir: Path
    worktree: Path


def resolve_repo(start: Path) -> RepoIdentity:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        )
        common, worktree = result.stdout.splitlines()
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise DomainError("INVALID_REPOSITORY", "Cannot resolve Git common directory and worktree") from exc
    return RepoIdentity(Path(common).resolve(), Path(worktree).resolve())


def resolve_artifact(repo: RepoIdentity, path: Path) -> Path:
    resolved = (path if path.is_absolute() else repo.worktree / path).resolve()
    if not resolved.is_relative_to(repo.worktree):
        raise DomainError("UNSAFE_PATH", "Artifact path escapes worktree")
    return resolved
