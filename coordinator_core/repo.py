import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import DomainError

# F24 (launch-boundary design L0): every Git call the coordinator makes in a worktree turns off the two commands a
# repository's own config can make Git run on reads, writes and merges: core.fsmonitor (run by ls-files and status)
# and the hooks (worktree add, merge, checkout). A worker that could write .git/config or .git/hooks never gets
# the coordinator to run its command after the run.
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


@dataclass(frozen=True)
class RepoIdentity:
    common_dir: Path
    worktree: Path


def resolve_repo(start: Path) -> RepoIdentity:
    try:
        result = subprocess.run(
            ["git", *GIT_SAFE, "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
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
