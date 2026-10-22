"""The launch boundary (launch-boundary design L2 to L8): one bubblewrap sandbox around a client process and all
its descendants. Every path is mounted at its host path, so an argv, a cwd and every receipt path mean the same
inside and outside; what is not mounted does not exist inside. Standard library only; `bwrap` is an external
executable, resolved and probed before any reservation (L7)."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import DomainError

KIND = "bwrap"
KINDS = (KIND,)
MODES = ("ro", "rw", "copy", "tmpfs", "symlink", "empty")
NAMESPACES = ("--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try")
MERGED_USR = ("bin", "lib", "lib64", "sbin")
ENV = {"GIT_OPTIONAL_LOCKS": "0"}   # L2: a read-only Git skips its optional index refresh
PROBE_SECONDS = 20
MAX_LINKS = 16


@dataclass(frozen=True)
class Mount:
    mode: str          # ro | rw | copy (a private 0600 writable copy of a file) | tmpfs | symlink | empty (an
                       # existing file shown empty and read-only; the host's file is never written)
    path: str          # absolute and normalized, the same inside as outside
    target: str = ""   # symlink only: the link's target, verbatim


def check_kind(value) -> str | None:
    """None (no boundary) or a supported kind; anything else is INVALID_INPUT (L1)."""
    if value is None:
        return None
    if value not in KINDS:
        raise DomainError("INVALID_INPUT", f"boundary {value!r} is not supported (one of {', '.join(KINDS)})")
    return value


def unusable(reason: str) -> DomainError:
    return DomainError("UNSUPPORTED_CLIENT_MODE", f"boundary {KIND} is not usable: {reason}")


def system_mounts() -> list[Mount]:
    """L2: the system read-only (merged-usr links recreated), /etc and the resolver's target, a fresh /tmp."""
    mounts = [Mount("ro", "/usr")]
    for name in MERGED_USR:
        path = Path("/") / name
        if path.is_symlink():
            mounts.append(Mount("symlink", str(path), os.readlink(path)))
        elif path.is_dir():
            mounts.append(Mount("ro", str(path)))
    mounts.append(Mount("ro", "/etc"))
    resolver = Path(os.path.realpath("/etc/resolv.conf"))
    if resolver.is_file() and not resolver.is_relative_to("/etc") and not resolver.is_relative_to("/usr"):
        mounts.append(Mount("ro", str(resolver)))
    return mounts + [Mount("tmpfs", "/tmp")]


def link_chain(path: str | os.PathLike) -> list[Mount]:
    """The symlinks from `path` to its real file, recreated inside, so an argv keeps its host spelling (L3)."""
    links, current = [], Path(os.path.abspath(path))
    while current.is_symlink() and len(links) < MAX_LINKS:
        target = os.readlink(current)
        links.append(Mount("symlink", str(current), target))
        current = Path(os.path.normpath(current.parent / target))
    return links


def executable(path: str | os.PathLike) -> list[Mount]:
    """L3: an executable as the launch spells it: its symlink chain and its real file's directory, read-only."""
    return [*link_chain(path), Mount("ro", str(Path(os.path.realpath(path)).parent))]


def interpreter(path: str | os.PathLike, search_path: str) -> list[Mount]:
    """L3: for a script, the interpreter its shebang names (looked up on `search_path` when it goes through
    `env`), mounted as `executable` mounts it, plus the `lib` beside its `bin` when that prefix is outside `/` and
    `/usr` (a Python's standard library, a Node's modules). A native executable has none."""
    try:
        with open(os.path.realpath(path), "rb") as stream:
            line = stream.readline(512)
    except OSError:
        return []
    words = line[2:].decode("utf-8", "replace").split() if line.startswith(b"#!") else []
    if not words:
        return []
    program, mounts = words[0], []
    if os.path.basename(program) == "env":
        mounts += executable(program) if os.path.isabs(program) else []
        names = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
        found = shutil.which(names[0], path=search_path) if names else None
        if not found:
            return mounts
        program = found
    if not os.path.isabs(program):
        return mounts
    mounts += executable(program)
    real = Path(os.path.realpath(program))
    prefix = real.parent.parent
    if real.parent.name == "bin" and prefix not in (Path("/"), Path("/usr")) and (prefix / "lib").is_dir():
        mounts.append(Mount("ro", str(prefix / "lib")))
    return mounts


def node_prefix(real: Path) -> Path | None:
    """The Node installation a globally installed script belongs to: the nearest ancestor P with `P/bin/node`
    under whose `lib/node_modules` the script lies (npm's global layout). A script elsewhere has none, so a
    system `/usr/bin/node` never makes `/` a prefix."""
    for parent in real.parents:
        if real.is_relative_to(parent / "lib" / "node_modules") and (parent / "bin" / "node").exists():
            return parent
    return None


def project_mounts(kind: str, run_dir: Path, cwd: Path, worktree: Path | None = None,
                   common_dir: Path | None = None, sealed: tuple = (), others: tuple = ()) -> list[Mount]:
    """L4. `isolated`: the cwd read-only and the receipt directory read-write, nothing of the project. `reviewer`:
    the worktree and the common directory read-only, the coordinator's directory an empty tmpfs but for the sealed
    copies (read-only) and the run directory. `writer`: the same with the worktree read-write and its Git entry
    read-only over it. Other worktrees nested in the mounted tree are hidden."""
    if kind == "isolated":
        return [Mount("ro", str(cwd)), Mount("rw", str(run_dir))]
    if kind not in ("reviewer", "writer") or worktree is None or common_dir is None:
        raise ValueError(f"unknown launch kind {kind!r}")
    mounts = [Mount("ro" if kind == "reviewer" else "rw", str(worktree)), Mount("ro", str(common_dir))]
    if kind == "writer":
        mounts.append(Mount("ro", str(worktree / ".git")))
    mounts += [Mount("tmpfs", str(path)) for path in others if Path(path).is_relative_to(worktree)]
    return mounts + [Mount("tmpfs", str(common_dir / "coordinator")), *[Mount("ro", str(path)) for path in sealed],
                     Mount("rw", str(run_dir))]


def check_outside(mounts: list[Mount], protected) -> None:
    """L2: a client-state, runtime or project mount whose real path equals, contains or lies inside a protected
    path (the common directory, the coordinator directory, a worktree's Git entry, another worktree) is refused,
    so an innocent-looking alias cannot expose what the launch kind hides."""
    guarded = [Path(os.path.realpath(item)) for item in protected]
    for mount in mounts:
        if mount.mode == "symlink":
            continue
        real = Path(os.path.realpath(mount.path))
        for path in guarded:
            if real == path or real.is_relative_to(path) or path.is_relative_to(real):
                raise unusable(f"{mount.path} reaches the protected {path}")


def ordered(mounts: list[Mount]) -> tuple[list[Mount], list[str]]:
    """L2: duplicates dropped (the same mode and path), parents before children (a bind refines the tmpfs or
    read-only tree above it), and paths that do not exist set aside as absent. Two modes for one path, or a
    relative or unnormalized path, are refused."""
    seen: dict[str, Mount] = {}
    for mount in mounts:
        if mount.mode not in MODES or not os.path.isabs(mount.path) or os.path.normpath(mount.path) != mount.path:
            raise unusable(f"bad mount {mount.mode} {mount.path}")
        earlier = seen.get(mount.path)
        if earlier is not None and earlier != mount:
            raise unusable(f"{mount.path} is mounted both {earlier.mode} and {mount.mode}")
        seen[mount.path] = mount
    present, absent = [], []
    binds = [Path(item.path) for item in seen.values() if item.mode in ("ro", "rw")]
    for mount in seen.values():
        if mount.mode in ("ro", "rw", "copy", "empty") and not os.path.lexists(mount.path):
            absent.append(mount.path)
        elif mount.mode == "symlink" and any(Path(mount.path).is_relative_to(path) for path in binds):
            continue   # the link already exists in that bind (a read-only one could not take a new one)
        else:
            present.append(mount)
    return sorted(present, key=lambda item: len(Path(item.path).parts)), sorted(absent)


def wrap(bwrap: str, mounts: list[Mount], cwd: Path, command: list[str], copies: dict[str, int]) -> list[str]:
    """L2/L6: the bwrap argv around `command`; no --new-session (L6) and no secret in it (L2)."""
    argv = [bwrap, "--die-with-parent", *NAMESPACES, "--proc", "/proc", "--dev", "/dev"]
    for mount in mounts:
        if mount.mode == "ro":
            argv += ["--ro-bind", mount.path, mount.path]
        elif mount.mode == "rw":
            argv += ["--bind", mount.path, mount.path]
        elif mount.mode == "tmpfs":
            argv += ["--tmpfs", mount.path]
        elif mount.mode == "symlink":
            argv += ["--symlink", mount.target, mount.path]
        elif mount.mode == "empty":   # bound over the file, so a read-write parent bind never writes it
            argv += ["--ro-bind-data", str(copies[mount.path]), mount.path]
        else:
            argv += ["--perms", "0600", "--file", str(copies[mount.path]), mount.path]
    return argv + ["--chdir", str(cwd), "--", *command]


def open_copies(mounts: list[Mount]) -> dict[str, int]:
    """Read-only descriptors for the `copy` mounts (the file) and the `empty` ones (`/dev/null`), opened just before
    a spawn; the caller passes them with `pass_fds` and closes them with `close` on every path (L3)."""
    copies = {}
    try:
        for mount in mounts:
            if mount.mode == "copy":
                copies[mount.path] = os.open(mount.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            elif mount.mode == "empty":
                copies[mount.path] = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        close(copies)
        raise
    return copies


def close(copies: dict[str, int]) -> None:
    for fd in copies.values():
        try:
            os.close(fd)
        except OSError:
            pass


def record(bwrap: str, version: str, mounts: list[Mount], absent: list[str], env_names, wrapped: list[str],
           **extra) -> dict:
    """L5: the `boundary` object of `launch.json`, inside the anchored launch digest. `wrapped` is the full argv
    with the envelope placeholder unexpanded and the copy descriptors as numbers."""
    return {"kind": KIND, "binary": bwrap, "version": version,
            "mounts": [{"mode": item.mode, "path": item.path, **({"target": item.target} if item.target else {})}
                       for item in mounts],
            "absent": list(absent), "env_names": sorted(env_names), "argv": list(wrapped), **extra}


def check_record(value, kind: str, client_argv: list) -> list[str]:
    """L5, at replay: what is wrong with a sealed boundary record for a launch that named `kind` (empty: nothing)."""
    if not isinstance(value, dict):
        return ["the launch receipt lacks the boundary record its binding names"]
    argv = value.get("argv")
    if kind not in KINDS or value.get("kind") != kind or not isinstance(argv, list) or \
       not isinstance(value.get("mounts"), list):
        return ["the launch receipt's boundary record is malformed or of another kind"]
    if not isinstance(client_argv, list) or "--" not in argv or argv[len(argv) - len(client_argv):] != client_argv \
       or argv[-len(client_argv) - 1] != "--":
        return ["the launch receipt's boundary argv does not end with the client argv"]
    return []


def _run(argv: list[str], env: dict, copies: dict[str, int] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_SECONDS, env=env,
                          stdin=subprocess.DEVNULL, pass_fds=tuple((copies or {}).values()))


def _last(text: str) -> str:
    return (text.strip().splitlines() or ["no output"])[-1][:300]


def preflight(env: dict) -> tuple[str, str]:
    """L7: the absolute `bwrap`, its version and a no-op sandbox, before any reservation; never a model call."""
    found = shutil.which("bwrap", path=env.get("PATH", os.defpath))
    if not found:
        raise unusable("bwrap is not on PATH")
    binary = os.path.abspath(found)
    try:
        result = _run([binary, "--version"], env)
        version = (result.stdout.strip().splitlines() or [""])[0].strip()
        if result.returncode != 0 or not version:
            raise unusable("bwrap --version failed")
        mounts, _ = ordered(system_mounts())
        noop = _run(wrap(binary, mounts, Path("/"), ["/bin/true"], {}), env)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise unusable(f"bwrap cannot run ({type(exc).__name__})") from exc
    if noop.returncode != 0:
        raise unusable(f"a no-op sandbox failed: {_last(noop.stderr)}")
    return binary, version


def probe(bwrap: str, mounts: list[Mount], cwd: Path, command: list[str], env: dict, what: str) -> None:
    """L7: one model-free command (a client's or uv's --version) inside the launch's full mount set."""
    copies = open_copies(mounts)
    try:
        result = _run(wrap(bwrap, mounts, cwd, command, copies), env, copies)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise unusable(f"{what} cannot start inside it ({type(exc).__name__})") from exc
    finally:
        close(copies)
    if result.returncode != 0:
        raise unusable(f"{what} fails inside it: {_last(result.stderr or result.stdout)}")
