"""BMAD snapshot design (issue #4): a gitignored BMAD install, carried into a feature worktree from a sealed,
content-addressed snapshot of the main worktree's install. Every read and write here is descriptor-relative and
follows no link; nothing here executes installed code except the discovery that proves a copy."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from . import bmad_contract as contract
from .model import DomainError
from .repo import GIT_SAFE, RepoIdentity
from .run_receipt import canonical

ROOTS = (".agents/skills", ".claude/skills")   # S1: the client roots, a closed set
INSTALL = "_bmad"
STORE = "bmad"   # under <common>/coordinator, a sealed top of the D16 baseline
LISTING = "snapshot.json"
SKIPPED = "__pycache__"
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def check_root(client_root: str) -> None:
    if client_root not in ROOTS:
        raise DomainError("INVALID_INPUT", f"Client root must be one of {', '.join(ROOTS)}")


def _tops(client_root: str) -> tuple[str, str]:
    return INSTALL, client_root


def _git(path: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_SAFE, "-C", str(path), *args], input=data, capture_output=True)


def main_worktree(repo: RepoIdentity) -> Path:
    """S2: the first entry of `git worktree list`, the repository's main worktree; a bare one has no install."""
    done = _git(repo.worktree, "worktree", "list", "--porcelain")
    first = done.stdout.decode("utf-8", "replace").split("\n\n")[0].splitlines() if done.returncode == 0 else []
    if not first or not first[0].startswith("worktree ") or "bare" in first:
        raise DomainError("BLOCKED_EVIDENCE", "The repository has no main worktree to take the BMAD install from")
    path = Path(first[0][len("worktree "):])
    if not path.is_dir():
        raise DomainError("BLOCKED_EVIDENCE", f"The main worktree is missing: {path}")
    return path


def classify(worktree: Path, client_root: str) -> str:
    """S3: `committed` when both roots are tracked in the commit and the index, `mixed` when anything of them is
    tracked or staged otherwise, else `absent`."""
    tracked = []
    for top in _tops(client_root):
        commit = _git(worktree, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", top)
        index = _git(worktree, "ls-files", "-z", "--", top)
        if commit.returncode != 0 or index.returncode != 0:
            raise DomainError("INVALID_REPOSITORY", "Cannot read the worktree's tracked files")
        tracked.append((bool(commit.stdout.strip(b"\0")), bool(index.stdout.strip(b"\0"))))
    if all(commit and index for commit, index in tracked):
        return "committed"
    return "mixed" if any(commit or index for commit, index in tracked) else "absent"


def uncovered(worktree: Path, paths: list[str]) -> list[str]:
    """S5: the paths the worktree's ignore rules do not ignore (a negation included)."""
    done = _git(worktree, "check-ignore", "--no-index", "-z", "--stdin", data=b"".join(
        os.fsencode(path) + b"\0" for path in paths))
    if done.returncode not in (0, 1):
        raise DomainError("INVALID_REPOSITORY", "Cannot read the worktree's ignore rules")
    ignored = {os.fsdecode(item) for item in done.stdout.split(b"\0") if item}
    return [path for path in paths if path not in ignored]


def _scan(root: Path, tops: tuple[str, ...]) -> tuple[dict, list[str]]:
    """{path: (sha256, executable, bytes)} of every regular single-link file under the tops, and the problems
    (a link, a special or hardlinked file, a non-directory on the way); an absent top holds nothing."""
    files, problems = {}, []

    def walk(fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(fd)):
            rel = f"{prefix}/{name}"
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if name == SKIPPED:
                    continue
                sub = os.open(name, DIRECTORY, dir_fd=fd)
                try:
                    walk(sub, rel)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(info.st_mode):
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        problems.append(f"not a regular single-link file: {rel}")
                        continue
                    data = stream.read()
                files[rel] = (hashlib.sha256(data).hexdigest(), bool(opened.st_mode & 0o111), data)
            else:
                problems.append(f"not a regular single-link file: {rel}")

    root_fd = os.open(root, DIRECTORY)
    try:
        for top in tops:
            fd, held = root_fd, []
            try:
                for index, part in enumerate(top.split("/")):
                    try:
                        info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        fd = None
                        break
                    if not stat.S_ISDIR(info.st_mode):
                        problems.append(f"not a directory: {'/'.join(top.split('/')[:index + 1])}")
                        fd = None
                        break
                    fd = os.open(part, DIRECTORY, dir_fd=fd)
                    held.append(fd)
                if fd is not None:
                    walk(fd, top)
            finally:
                for item in held:
                    os.close(item)
    finally:
        os.close(root_fd)
    return files, problems


def _differences(files: dict, problems: list[str], listing: dict) -> list[str]:
    expected = {item["path"]: item for item in listing["files"]}
    found = list(problems)
    for path in sorted(set(expected) | set(files)):
        if path not in files:
            found.append(f"removed: {path}")
        elif path not in expected:
            found.append(f"added: {path}")
        else:
            if files[path][0] != expected[path]["sha256"]:
                found.append(f"changed: {path}")
            if files[path][1] != expected[path]["executable"]:
                found.append(f"mode: {path}")
    return found


def compare(worktree: Path, listing: dict) -> list[str]:
    """S7: the worktree's two roots against a snapshot listing, without executing anything; [] when equal."""
    files, problems = _scan(Path(worktree), _tops(listing["client_root"]))
    return _differences(files, problems, listing)


def _store(common_dir: Path) -> Path:
    return Path(common_dir) / "coordinator" / STORE


def verify_store(common_dir: Path, tree: str) -> dict:
    """S2, S7: the stored snapshot `tree`, every file checked against its listing, no extra entry; the listing."""
    if not isinstance(tree, str) or len(tree) != 64 or set(tree) - set("0123456789abcdef"):
        raise DomainError("UNKNOWN_EVIDENCE", "Snapshot id is not a SHA-256")
    base = _store(common_dir) / tree
    try:
        fd = os.open(base / LISTING, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read()
        listing = json.loads(data)
        if hashlib.sha256(data).hexdigest() != tree or canonical(listing) != data:
            raise ValueError("listing digest differs")
        tops = _tops(listing["client_root"])
        check_root(listing["client_root"])
        files, problems = _scan(base, tops)
        entries = sorted(os.listdir(base))
    except (OSError, ValueError, KeyError, TypeError, DomainError) as exc:
        raise DomainError("UNKNOWN_EVIDENCE", f"Snapshot {tree} is missing or unreadable") from exc
    if set(entries) - {LISTING, *(top.split("/")[0] for top in tops)} or _differences(files, problems, listing):
        raise DomainError("UNKNOWN_EVIDENCE", f"Snapshot {tree} differs from its listing")
    return listing


def _write_file(root: Path, rel: str, data: bytes, executable: bool, created: list[str]) -> None:
    """One file under `root`, every directory on the way opened without following a link and created when absent;
    each path this call creates is appended to `created`."""
    parts = rel.split("/")
    fd, held = os.open(root, os.O_RDONLY | os.O_DIRECTORY), []
    try:
        held.append(fd)
        for index, part in enumerate(parts[:-1]):
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
                created.append("/".join(parts[:index + 1]))
            except FileExistsError:
                pass
            try:
                fd = os.open(part, DIRECTORY, dir_fd=fd)
            except OSError as exc:
                raise DomainError("UNSAFE_PATH", f"Not a plain directory: {'/'.join(parts[:index + 1])}") from exc
            held.append(fd)
        file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created.append(rel)
        with os.fdopen(file_fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), 0o755 if executable else 0o644)
    finally:
        for item in held:
            os.close(item)


def _remove(root: Path, created: list[str]) -> None:
    for rel in reversed(created):
        path = Path(root) / rel
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError:
            pass


def take_snapshot(repo: RepoIdentity, client_root: str) -> str:
    """S1, S2: the main worktree's install as a stored snapshot; its id, the digest of its canonical listing."""
    check_root(client_root)
    source = main_worktree(repo)
    report = contract.discover(source, client_root)
    if not report.supported:
        raise DomainError("UNSUPPORTED_CONTRACT", "; ".join(report.reasons))
    files, problems = _scan(source, _tops(client_root))
    if problems:
        raise DomainError("UNSAFE_PATH", "The BMAD install has a link, a special or a hardlinked file: " +
                          "; ".join(problems[:10]))
    listing = {"format": 1, "client_root": client_root, "fingerprint_sha256": report.fingerprint_sha256,
               "files": [{"path": path, "sha256": files[path][0], "executable": files[path][1]}
                         for path in sorted(files)]}
    data = canonical(listing)
    tree = hashlib.sha256(data).hexdigest()
    store = _store(repo.common_dir)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.path.lexists(store / tree):
        verify_store(repo.common_dir, tree)
        return tree
    staging = store / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for path in sorted(files):
            _write_file(staging, path, files[path][2], files[path][1], [])
        _write_file(staging, LISTING, data, False, [])
        os.rename(staging, store / tree)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    verify_store(repo.common_dir, tree)
    return tree


def _proved(worktree: Path, listing: dict) -> None:
    """S3: the copy is the snapshot, and discovery on it gives the snapshot's fingerprint."""
    differences = compare(worktree, listing)
    if differences:
        raise DomainError("UNKNOWN_EVIDENCE", "The materialized install differs from its snapshot: " +
                          "; ".join(differences[:10]))
    report = contract.discover(worktree, listing["client_root"])
    if not report.supported:
        raise DomainError("UNSUPPORTED_CONTRACT", "; ".join(report.reasons))
    if report.fingerprint_sha256 != listing["fingerprint_sha256"]:
        raise DomainError("UNKNOWN_EVIDENCE", "The materialized install's fingerprint differs from its snapshot")


def materialize(repo: RepoIdentity, client_root: str, tree: str) -> bool:
    """S3, S5: the snapshot `tree` copied into the worktree and proved; False when it already holds exactly it.
    A failure removes exactly the paths this call created."""
    check_root(client_root)
    listing = verify_store(repo.common_dir, tree)
    if listing["client_root"] != client_root:
        raise DomainError("INVALID_INPUT", "The snapshot was taken for another client root")
    worktree = repo.worktree
    for top in _tops(client_root):
        parts = top.split("/")
        for index in range(1, len(parts) + 1):
            path = worktree / "/".join(parts[:index])
            if path.is_symlink() or (os.path.lexists(path) and not path.is_dir()):
                raise DomainError("UNSAFE_PATH", f"Not a plain directory: {'/'.join(parts[:index])}")
    state = classify(worktree, client_root)
    if state != "absent":
        raise DomainError("BLOCKED_EVIDENCE", "The BMAD install is committed in this worktree: nothing to materialize"
                          if state == "committed" else "Part of the BMAD install is tracked or staged in this "
                          "worktree: commit all of it or none")
    escaped = uncovered(worktree, [item["path"] for item in listing["files"]])
    if escaped:
        raise DomainError("UNSAFE_PATH", f"Not ignored in this worktree: {', '.join(escaped[:10])}; the ignore "
                          f"rules must cover every file (`{INSTALL}/` and `{client_root}/` in .gitignore)")
    if any(os.path.lexists(worktree / top) for top in _tops(client_root)):
        differences = compare(worktree, listing)
        if differences:
            raise DomainError("BLOCKED_EVIDENCE", "The worktree's BMAD install differs from the snapshot: " +
                              "; ".join(differences[:10]))
        _proved(worktree, listing)
        return False
    store = _store(repo.common_dir) / tree
    created: list[str] = []
    try:
        for item in listing["files"]:
            with os.fdopen(os.open(store / item["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                opened = os.fstat(stream.fileno())
                data = stream.read() if stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 else None
            if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise DomainError("UNKNOWN_EVIDENCE", f"Snapshot {tree} changed during the copy")
            _write_file(worktree, item["path"], data, item["executable"], created)
        _proved(worktree, listing)
    except BaseException:
        _remove(worktree, created)
        raise
    return True


EVENT = "bmad.materialized"   # S7: the log of a materialization; the binding is the authority


def record(repo: RepoIdentity, store, owner, feature_id: str, tree: str) -> None:
    from .service import Coordinator   # lazy: the service module is the store's writer
    Coordinator(store).anchor_event(owner, EVENT, feature_id, (str(repo.worktree), f"{STORE}/{tree}/{LISTING}", tree))


def materialized(events: list[dict], worktree: Path) -> dict | None:
    """The newest materialization recorded for this worktree, as {"tree", "feature_id"}; None when there is none."""
    found = [item for item in events if item["kind"] == EVENT and item["source_id"] == str(worktree)]
    return {"tree": found[-1]["payload_digest"], "feature_id": found[-1]["subject_id"]} if found else None


def doctor(repo: RepoIdentity, client_root: str) -> dict:
    """S5: the worktree's install state for `doctor --bmad`; a non-empty `reasons` makes it not ready."""
    check_root(client_root)
    state, reasons = classify(repo.worktree, client_root), []
    if state == "mixed":
        reasons.append("Part of the BMAD install is tracked or staged in this worktree: commit all of it or none")
    elif state == "absent":
        files, problems = _scan(repo.worktree, _tops(client_root))
        if not files and not problems:
            files, problems = _scan(main_worktree(repo), _tops(client_root))
        reasons += problems
        escaped = uncovered(repo.worktree, sorted(files))
        state = "not-ignored" if escaped else "ignored"
        if not files and not problems:
            state, reasons = "absent", ["No BMAD install in this worktree or the main worktree"]
        elif escaped:
            reasons.append(f"Not ignored in this worktree: {', '.join(escaped[:5])}; add `{INSTALL}/` and "
                           f"`{client_root}/` to .gitignore")
    return {"state": state, "client_root": client_root, "reasons": reasons}


def materialize_command(repo: RepoIdentity, store, owner, client_root: str, tree: str | None) -> dict:
    """S10: `bmad materialize` in the feature's own worktree, then the event; idempotent."""
    from .service import require_open_feature
    check_root(client_root)
    features = store.read_export_snapshot(owner)["features"]   # authenticates the owner first
    # by path, never by running Git in each one: a closed feature's worktree may be gone (issue #8); every feature's
    # recorded worktree is its resolved top level, in this repository
    matching = [row for row in features if row["worktree"] and Path(row["worktree"]).resolve() == repo.worktree]
    # an open feature first: a worktree may be reused by a new feature after its old one closed
    feature = next((row for row in matching if row["state"] in ("active", "unresolved")),
                   matching[0] if matching else None)
    if feature is None:
        raise DomainError("INVALID_REPOSITORY", "Materialize must run in a feature's recorded worktree")
    require_open_feature({"features": features}, feature["id"])   # feature lifecycle F5: before anything is written
    tree = tree if tree is not None else take_snapshot(repo, client_root)
    written = materialize(repo, client_root, tree)
    record(repo, store, owner, feature["id"], tree)
    return {"tree": tree, "fingerprint_sha256": verify_store(repo.common_dir, tree)["fingerprint_sha256"],
            "client_root": client_root, "written": written}


def guard(worktree: Path, common_dir: Path, events: list[dict], tree: str | None = None,
          code: str = "STALE_INPUT") -> dict | None:
    """S7, before any installed code runs: a materialized worktree's roots must equal its snapshot (compared without
    executing anything), and a binding's snapshot must be the one recorded. The `bmad_source` for a binding, or None
    for a worktree that was never materialized."""
    found = materialized(events, Path(worktree))
    if found is None:
        if tree is not None:
            raise DomainError(code, "The binding names a BMAD snapshot this worktree has no materialization of")
        return None
    if tree is not None and found["tree"] != tree:
        raise DomainError(code, "The worktree was materialized from another BMAD snapshot than the binding's")
    listing = verify_store(common_dir, found["tree"])
    differences = compare(Path(worktree), listing)
    if differences:
        raise DomainError(code, "The materialized BMAD install changed: " + "; ".join(differences[:10]))
    return {"kind": "snapshot", "tree": found["tree"], "client_root": listing["client_root"]}
