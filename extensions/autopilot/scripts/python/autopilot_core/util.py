"""Small shared helpers: atomic JSON files, subprocesses, git, locks, timestamps."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

STATE_DIR = Path(".specify") / "autopilot"
# Checkout-local runtime state the autopilot never commits, reviews or fingerprints.
RUNTIME_PATHS = (".specify/autopilot", ".specify/workflows/runs", ".specify/feature.json")
EXCLUDED_FROM_COMMITS = RUNTIME_PATHS
SYNCED_MANIFEST = "synced.json"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_id(value: Any) -> bool:
    """A single path- and command-safe identifier (question, decision, epic, run IDs)."""
    return isinstance(value, str) and SAFE_ID_RE.fullmatch(value) is not None and ".." not in value


STATE_GITIGNORE = (
    "# Autopilot run state is local to this checkout.\n*\n"
    "!.gitignore\n!epics.json\n!epics.md\n!dag.md\n!report.md\n"
)


def ensure_state_dir(project_root: Path) -> Path:
    """Create ``.specify/autopilot`` with a catch-all ``.gitignore``."""
    path = project_root / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text(STATE_GITIGNORE, encoding="utf-8")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_lenient(path: Path, default: Any = None) -> Any:
    """``read_json`` that treats an unreadable or half-written file as missing."""
    try:
        return read_json(path, default)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"at": now_iso(), **record}, sort_keys=False) + "\n")


# ── processes ────────────────────────────────────────────────────────────────


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a process and everything it started (its own process group / job)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        # Whatever is left of the group (descendants that ignored SIGTERM).
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(
    args: list[str] | str,
    cwd: Path,
    *,
    shell: bool = False,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a process in its own process group; decode as UTF-8; never raise on non-zero exit.

    On timeout the whole process tree is terminated and reaped, so agent CLIs or
    checks started by the command cannot keep editing files afterwards.
    """
    merged_env = {**os.environ, **env} if env is not None else None
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            args, cwd=str(cwd), shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=merged_env, **kwargs,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", f"[autopilot] could not start: {exc}")
    if on_start is not None:
        on_start(process.pid)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # A descendant escaped the group and still holds the pipes: stop waiting.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            process.wait()
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(args, -1, stdout or "", (stderr or "") + f"\n[autopilot] timed out after {timeout}s")
    except BaseException:
        _kill_tree(process)
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group(pid: int) -> None:
    """Terminate a process group this checkout started earlier (an orphaned pipe)."""
    if pid <= 0 or not pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(50):
            if not pid_alive(pid):
                return
            time.sleep(0.1)


# ── locks ────────────────────────────────────────────────────────────────────


class LockHeld(RuntimeError):
    def __init__(self, owner: dict[str, Any]) -> None:
        super().__init__(f"held by pid {owner.get('pid')} since {owner.get('started_at')}")
        self.owner = owner


def _try_lock(fd: int, shared: bool = False) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class ProcessLock:
    """An exclusive, OS-held lock for one long-running process per checkout.

    The operating system releases it when the holder exits or crashes, so there
    is no stale-owner takeover to race on. The file also records the holder's
    PID for status reports.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            raise LockHeld(self.owner() or {})
        info = json.dumps({"pid": os.getpid(), "started_at": now_iso()}).encode()
        # The first byte stays reserved for the Windows region lock: write the info after it.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b" " + info + b"\n")
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

    def owner(self) -> dict[str, Any] | None:
        """The live holder, or None. Never reports a crashed holder."""
        if self._fd is not None:
            return {"pid": os.getpid()}
        if not self.path.is_file():
            return None
        fd = os.open(self.path, os.O_RDWR)
        try:
            if _try_lock(fd, shared=True):
                return None  # nobody holds it (closing fd drops our probe lock)
            os.lseek(fd, 1, os.SEEK_SET)  # byte 0 is the (Windows) lock region
            raw = os.read(fd, 4096).strip()
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    def acquire_wait(self, timeout: float = 30.0) -> None:
        """Block (polling) until the lock is free, for short critical sections."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.acquire()
                return
            except LockHeld:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def __enter__(self) -> "ProcessLock":
        self.acquire_wait()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ── git ──────────────────────────────────────────────────────────────────────


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git; return stdout without trailing whitespace (leading columns are kept)."""
    result = run(["git", *args], cwd)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.rstrip()


def git_ok(cwd: Path, *args: str) -> bool:
    return run(["git", *args], cwd).returncode == 0


def repo_root(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel"))


def _under(path: str, prefixes: tuple[str, ...] | list[str]) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def is_runtime_path(path: str) -> bool:
    return _under(path, RUNTIME_PATHS)


def status_entries(cwd: Path, *pathspec: str) -> list[tuple[str, str]]:
    """``(code, path)`` pairs from ``git status --porcelain -z`` (no quoting, renames resolved)."""
    raw = run(["git", "status", "--porcelain", "-z", "--untracked-files=all", *(("--", *pathspec) if pathspec else ())], cwd)
    if raw.returncode != 0:
        raise GitError(f"git status failed in {cwd}: {raw.stderr.strip()}")
    parts = raw.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        entries.append((code, path))
        if code[0] in "RC":
            index += 1  # the rename/copy source follows as its own field
    return entries


def dirty_paths(cwd: Path, allow_untracked: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Changed paths, ignoring autopilot runtime state and untracked files under ``allow_untracked``."""
    dirty = []
    for code, path in status_entries(cwd):
        if is_runtime_path(path) or (code == "??" and _under(path, allow_untracked)):
            continue
        dirty.append(path)
    return dirty


def is_clean(cwd: Path, allow_untracked: tuple[str, ...] | list[str] = ()) -> bool:
    return not dirty_paths(cwd, allow_untracked)


def exclude_pathspecs() -> list[str]:
    return [f":(exclude){path}" for path in RUNTIME_PATHS]


def tree_fingerprint(cwd: Path) -> str:
    """Hash of HEAD plus the working tree's changes, ignoring runtime state.

    Includes the commit so a lead or reviewer that *commits* edits is caught too,
    and excludes workflow checkpoints and autopilot state, which change between
    steps without anyone editing the project.
    """
    import hashlib

    excludes = ["--", ".", *exclude_pathspecs()]
    digest = hashlib.sha256()
    digest.update(git(cwd, "rev-parse", "HEAD", check=False).encode())
    digest.update(run(["git", "status", "--porcelain", "-z", "--untracked-files=all", *excludes], cwd).stdout.encode())
    digest.update(run(["git", "diff", "HEAD", "--binary", *excludes], cwd).stdout.encode())
    untracked = run(["git", "ls-files", "--others", "--exclude-standard", "-z", *excludes], cwd).stdout
    for line in sorted(p for p in untracked.split("\0") if p):
        target = cwd / line
        if target.is_file():
            digest.update(line.encode())
            digest.update(target.read_bytes())
    return digest.hexdigest()


def identity_args(cwd: Path) -> list[str]:
    """``-c user.*`` fallbacks so commits work in repos without an identity."""
    name = git(cwd, "config", "user.name", check=False) or "Spec Kit Autopilot"
    email = git(cwd, "config", "user.email", check=False) or "autopilot@localhost"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def synced_files(cwd: Path) -> set[str] | None:
    """Files the scheduler copied into this worktree, or None outside a scheduled worktree."""
    data = read_json_lenient(cwd / STATE_DIR / SYNCED_MANIFEST)
    if isinstance(data, list):
        return {p for p in data if isinstance(p, str)}
    return None


def record_synced(cwd: Path, paths: set[str]) -> None:
    existing = synced_files(cwd) or set()
    ensure_state_dir(cwd)
    write_json(cwd / STATE_DIR / SYNCED_MANIFEST, sorted(existing | paths))


def _local_setup(path: str, synced: set[str] | None, sync_paths: tuple[str, ...] | list[str]) -> bool:
    """A new file that is local setup and must stay out of the epic's commits.

    Spec Kit's own directory never holds an epic's deliverables (new files there
    are installed extensions, caches, checkpoints). Elsewhere, in a scheduled
    worktree exactly the files the scheduler copied in are local, so a new file
    an epic adds under, say, ``.github/skills/`` is committed and reviewed.
    """
    if _under(path, (".specify",)):
        return True
    if synced is not None:
        return path in synced
    return _under(path, sync_paths)  # standalone checkout: keep the directory rule


def _git_paths(cwd: Path, *args: str) -> list[str]:
    return [p for p in run(["git", *args, "-z"], cwd).stdout.split("\0") if p]


def stage_changes(cwd: Path, sync_paths: tuple[str, ...] | list[str] = ()) -> None:
    """Stage the epic's work and nothing else.

    Stages tracked changes and new files, except the setup files the scheduler
    copied in (they would collide with the same untracked copies in other
    checkouts at merge time) and runtime state. Anything an agent staged under
    runtime paths, or a copied setup file, is unstaged again, so the index
    holds exactly what the review sees and the commit records.
    """
    synced = synced_files(cwd)
    git(cwd, "add", "-u", "--", ".", *exclude_pathspecs())
    new_files = [
        path for path in _git_paths(cwd, "ls-files", "--others", "--exclude-standard")
        if not is_runtime_path(path) and not _local_setup(path, synced, sync_paths)
    ]
    for start in range(0, len(new_files), 200):
        git(cwd, "--literal-pathspecs", "add", "--", *new_files[start:start + 200])
    unwanted = [
        path for path in _git_paths(cwd, "diff", "--cached", "--name-only", "--no-renames")
        if is_runtime_path(path) or (path in (synced or set()))
        or (_under(path, (".specify",)) and not git_ok(cwd, "cat-file", "-e", f"HEAD:{path}"))
    ]
    for start in range(0, len(unwanted), 200):
        # Reset to HEAD in the index only; the working-tree files stay as they are.
        if git_ok(cwd, "rev-parse", "--verify", "HEAD"):
            git(cwd, "--literal-pathspecs", "reset", "-q", "HEAD", "--", *unwanted[start:start + 200])
        else:
            git(cwd, "--literal-pathspecs", "rm", "-q", "--cached", "--", *unwanted[start:start + 200])


def runtime_paths_between(cwd: Path, base: str, head: str) -> list[str]:
    """Runtime-state paths a branch changes relative to ``base`` (must be empty to merge)."""
    return [p for p in _git_paths(cwd, "diff", "--name-only", "--no-renames", f"{base}...{head}") if is_runtime_path(p)]


def commit_all(cwd: Path, message: str, sync_paths: tuple[str, ...] | list[str] = ()) -> bool:
    """Stage the epic's work (see ``stage_changes``) and commit; False if nothing to commit."""
    stage_changes(cwd, sync_paths)
    if git_ok(cwd, "diff", "--cached", "--quiet"):
        return False
    # Commit hooks are part of the project's environment constraints: never bypass them.
    git(cwd, *identity_args(cwd), "commit", "-q", "-m", message)
    return True
