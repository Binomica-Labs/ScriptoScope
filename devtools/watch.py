#!/usr/bin/env python3
"""
ScriptoScope Developer Daemon — devtools/watch.py
==================================================
This file is the __main__.py (named watch.py, zipped in as __main__.py)
embedded inside developer-daemon.com, an Actually Portable Executable built
with Cosmopolitan Python.

It is the ScriptoScope file-watcher daemon: it watches the repository for
changes and automatically kicks off the correct rebuild/reinstall action,
mirroring the manual developer workflows documented in DEVELOPERS.md.

Watched files → actions
-----------------------

  scriptoscope.py              →  pipx install . --force
                                   (reinstall the app so the new code is live)

  pyproject.toml               →  pipx install . --force
  requirements.txt                pipx inject scriptoscope -r requirements.txt --force
                                   (reinstall + sync deps when either manifest changes)

  devtools/__main__.py         →  sh devtools/build-ape.sh
  devtools/watch.py               (rebuild both APE binaries with the new source)
  devtools/build-ape.sh

NOTE: The watcher runs using the *system* Python that was installed by
setup-dev-env.com, NOT the Cosmopolitan Python bundled inside developer-daemon.com.
The Cosmopolitan Python is used only to bootstrap the watcher process;
it immediately re-execs via the system Python so that platform-native file
watching APIs (kqueue on macOS/BSD, inotify on Linux) are available.

Platform-native watching strategy:
  macOS / FreeBSD / OpenBSD / NetBSD  — kqueue (select.kqueue)
  Linux                               — inotify via /proc/sys/fs/inotify or
                                        ctypes libc, falling back to poll
  Windows                             — ReadDirectoryChangesW via ctypes,
                                        falling back to stat polling
  Fallback (any platform)             — stat-polling at 300 ms intervals

Usage:
  ./developer-daemon.com [OPTIONS] [REPO_PATH]

  REPO_PATH defaults to the repository root (auto-detected from script location).

Options:
  --poll               Force stat-polling even when a native backend is available
  --interval SECONDS   Polling interval (default: 0.3); only used with --poll
  --no-tests           Pass --no-tests to pipx install / rebuild steps
  --no-color           Disable ANSI color output
  -q, --quiet          Suppress informational output (errors still shown)
  -h, --help           Show this help message and exit

Exit codes:
  0    user pressed Ctrl-C (clean shutdown)
  1    fatal error during startup
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import queue
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WATCHER_VERSION = "1.0.0"

# Files we care about, relative to the repo root.
# App source files sit at the root; devtools source files are in devtools/.
# These strings are also the keys in ACTION_MAP and what the debouncer receives.
WATCHED_FILES = [
    "scriptoscope.py",
    "pyproject.toml",
    "requirements.txt",
    "devtools/__main__.py",
    "devtools/watch.py",
    "devtools/build-ape.sh",
    "devtools/sitecustomize.py",
]

# Debounce: after a change is detected, wait this long for the filesystem to
# settle before acting.  Editors often write in two steps (truncate + write).
DEBOUNCE_SECONDS = 0.5

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_COLOR = True  # toggled by --no-color / tty detection


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _bold(t: str) -> str:
    return _c("1", t)


def _dim(t: str) -> str:
    return _c("2", t)


def _red(t: str) -> str:
    return _c("31", t)


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _cyan(t: str) -> str:
    return _c("36", t)


def _magenta(t: str) -> str:
    return _c("35", t)


def ts() -> str:
    """Return a dim HH:MM:SS timestamp string."""
    return _dim(time.strftime("%H:%M:%S"))


def log_info(msg: str) -> None:
    print(f"{ts()} {_cyan('[watch]')} {msg}", flush=True)


def log_ok(msg: str) -> None:
    print(f"{ts()} {_green('[  ok ]')} {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"{ts()} {_yellow('[ warn]')} {msg}", flush=True, file=sys.stderr)


def log_err(msg: str) -> None:
    print(f"{ts()} {_red('[error]')} {msg}", flush=True, file=sys.stderr)


def log_action(msg: str) -> None:
    print(f"\n{ts()} {_bold(_magenta('[ run ]'))} {msg}", flush=True)


def log_section(msg: str) -> None:
    bar = "─" * min(72, len(msg) + 4)
    print(f"\n{_bold(bar)}", flush=True)
    print(f"{_bold('  ' + msg)}", flush=True)
    print(f"{_bold(bar)}", flush=True)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

_SYSTEM = platform.system().lower()
IS_MACOS = _SYSTEM == "darwin"
IS_LINUX = _SYSTEM == "linux"
IS_WINDOWS = _SYSTEM == "windows"
IS_BSD = any(bsd in _SYSTEM for bsd in ("bsd", "dragonfly"))


# ---------------------------------------------------------------------------
# Fingerprinting — used by the polling backend and change detection
# ---------------------------------------------------------------------------


def fingerprint(path: Path) -> tuple[int, int] | None:
    """Return (mtime_ns, size) or None if the file doesn't exist."""
    try:
        s = path.stat()
        return (s.st_mtime_ns, s.st_size)
    except (FileNotFoundError, PermissionError):
        return None


def content_hash(path: Path) -> str | None:
    """SHA-256 of the file content, or None on error.  Used for dedup."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Action definitions — what to run when a file changes
# ---------------------------------------------------------------------------


class Action:
    """
    An Action describes what to run in response to a file change.

    name        — short human-readable label shown in the terminal
    description — longer description for the header
    command_fn  — callable(repo, args) -> list[list[str]]  (list of commands to run in order)
    """

    def __init__(
        self,
        name: str,
        description: str,
        command_fn: Callable[[Path, argparse.Namespace], list[list[str]]],
    ) -> None:
        self.name = name
        self.description = description
        self.command_fn = command_fn


def _pipx_cmd() -> str:
    """Return 'pipx' if on PATH, else a reasonable fallback."""
    return shutil.which("pipx") or "pipx"


def rel_key(repo: Path, path: Path) -> str:
    """Return the WATCHED_FILES key for a path — e.g. 'devtools/__main__.py'."""
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.name


def _venv_python(repo: Path) -> str | None:
    """Return the path to the Python inside the scriptoscope pipx venv."""
    if IS_WINDOWS:
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
        p = (
            base
            / ".local"
            / "pipx"
            / "venvs"
            / "scriptoscope"
            / "Scripts"
            / "python.exe"
        )
    else:
        pipx_home = Path(os.environ.get("PIPX_HOME", Path.home() / ".local" / "pipx"))
        p = pipx_home / "venvs" / "scriptoscope" / "bin" / "python"
    return str(p) if p.exists() else None


def _action_reinstall_app(repo: Path, args: argparse.Namespace) -> list[list[str]]:
    """pipx install . --force"""
    pipx = _pipx_cmd()
    return [[pipx, "install", str(repo), "--force"]]


def _action_reinstall_deps(repo: Path, args: argparse.Namespace) -> list[list[str]]:
    """pipx install . --force  +  pipx inject ... -r requirements.txt --force"""
    pipx = _pipx_cmd()
    req = repo / "requirements.txt"
    cmds: list[list[str]] = [
        [pipx, "install", str(repo), "--force"],
    ]
    if req.exists():
        cmds.append([pipx, "inject", "scriptoscope", "-r", str(req), "--force"])
    return cmds


def _action_rebuild_ape(repo: Path, args: argparse.Namespace) -> list[list[str]]:
    """sh devtools/build-ape.sh  (rebuild both APE binaries)"""
    build_script = repo / "devtools" / "build-ape.sh"
    sh = shutil.which("sh") or "sh"
    return [[sh, str(build_script)]]


# Map: watched filename → Action
# When multiple watched files share an action, the action runs once per
# debounce window, not once per changed file.

ACTION_MAP: dict[str, Action] = {
    "scriptoscope.py": Action(
        name="reinstall app",
        description="scriptoscope.py changed → reinstalling via pipx",
        command_fn=_action_reinstall_app,
    ),
    "pyproject.toml": Action(
        name="reinstall + sync deps",
        description="pyproject.toml changed → reinstalling + syncing dependencies",
        command_fn=_action_reinstall_deps,
    ),
    "requirements.txt": Action(
        name="reinstall + sync deps",
        description="requirements.txt changed → reinstalling + syncing dependencies",
        command_fn=_action_reinstall_deps,
    ),
    "devtools/__main__.py": Action(
        name="rebuild APE",
        description="devtools/__main__.py changed → rebuilding APE binaries",
        command_fn=_action_rebuild_ape,
    ),
    "devtools/watch.py": Action(
        name="rebuild APE",
        description="devtools/watch.py changed → rebuilding APE binaries",
        command_fn=_action_rebuild_ape,
    ),
    "devtools/build-ape.sh": Action(
        name="rebuild APE",
        description="devtools/build-ape.sh changed → rebuilding APE binaries",
        command_fn=_action_rebuild_ape,
    ),
    "devtools/sitecustomize.py": Action(
        name="rebuild APE",
        description="devtools/sitecustomize.py changed → rebuilding APE binaries",
        command_fn=_action_rebuild_ape,
    ),
}


# ---------------------------------------------------------------------------
# Action runner — runs in a dedicated thread, serialises all actions
# ---------------------------------------------------------------------------


class ActionRunner(threading.Thread):
    """
    Consumes (action, repo, args, changed_files) tuples from a queue and runs
    them one at a time.  Runs as a daemon thread so it doesn't block Ctrl-C.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(daemon=True, name="action-runner")
        self._queue: queue.Queue[
            tuple[Action, Path, argparse.Namespace, list[str]] | None
        ] = queue.Queue()
        self._args = args
        self._running = True

    def submit(self, action: Action, repo: Path, changed: list[str]) -> None:
        self._queue.put((action, repo, self._args, changed))

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)  # sentinel

    def run(self) -> None:
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            action, repo, args, changed = item
            self._execute(action, repo, args, changed)

    def _execute(
        self,
        action: Action,
        repo: Path,
        args: argparse.Namespace,
        changed: list[str],
    ) -> None:
        changed_str = ", ".join(_cyan(f) for f in changed)
        log_section(f"{action.description}")
        log_info(f"Changed: {changed_str}")

        commands = action.command_fn(repo, args)
        all_ok = True
        t_start = time.monotonic()

        for cmd in commands:
            log_action(" ".join(cmd))
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(repo),
                    text=True,
                )
                if result.returncode != 0:
                    log_err(
                        f"Command failed with exit code {result.returncode}: "
                        + " ".join(cmd)
                    )
                    all_ok = False
                    break
            except FileNotFoundError as exc:
                log_err(f"Command not found: {exc}")
                all_ok = False
                break
            except Exception as exc:
                log_err(f"Unexpected error running command: {exc}")
                all_ok = False
                break

        elapsed = time.monotonic() - t_start
        if all_ok:
            log_ok(
                f"{_bold(action.name)} completed in {elapsed:.1f}s"
                + (" — watching for more changes…" if not args.quiet else "")
            )
        else:
            log_warn(
                f"{_bold(action.name)} failed after {elapsed:.1f}s"
                " — fix the error and save again to retry"
            )


# ---------------------------------------------------------------------------
# Debouncer — coalesces rapid file-system events into a single action
# ---------------------------------------------------------------------------


class Debouncer:
    """
    Collects changed filenames within a DEBOUNCE_SECONDS window, then
    resolves them to a single Action and submits it to the ActionRunner.

    When multiple changed files map to different actions, the action with the
    highest priority (first in PRIORITY) is chosen.
    """

    # Priority order: if multiple actions are triggered, run the first one
    # in this list (they're ordered by how much work they do — more expensive
    # actions subsume cheaper ones).
    PRIORITY = [
        "rebuild APE",
        "reinstall + sync deps",
        "reinstall app",
    ]

    def __init__(
        self, runner: ActionRunner, repo: Path, args: argparse.Namespace
    ) -> None:
        self._runner = runner
        self._repo = repo
        self._args = args
        self._lock = threading.Lock()
        self._pending_files: set[str] = set()
        self._timer: threading.Timer | None = None

    def notify(self, rel_path: str) -> None:
        """Called from any thread when a watched file changes.

        rel_path is the WATCHED_FILES key, e.g. 'devtools/__main__.py' or
        'scriptoscope.py'.  It must match an ACTION_MAP key to trigger an action.
        """
        with self._lock:
            self._pending_files.add(rel_path)
            # Reset the debounce window.
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            changed = list(self._pending_files)
            self._pending_files.clear()
            self._timer = None

        if not changed:
            return

        # Resolve all changed files to their actions.
        # changed entries are relative repo paths matching ACTION_MAP keys.
        actions: dict[str, tuple[Action, list[str]]] = {}
        for fname in changed:
            action = ACTION_MAP.get(fname)
            if action is None:
                continue
            key = action.name
            if key not in actions:
                actions[key] = (action, [])
            actions[key][1].append(fname)

        if not actions:
            return

        # Pick the highest-priority action.
        chosen_action: Action | None = None
        chosen_files: list[str] = []
        for priority_name in self.PRIORITY:
            if priority_name in actions:
                chosen_action, chosen_files = actions[priority_name]
                break

        # Include all changed filenames in the report even if subsumed.
        all_changed = sorted({f for files in actions.values() for f in files[1]})

        if chosen_action is not None:
            self._runner.submit(chosen_action, self._repo, all_changed)


# ---------------------------------------------------------------------------
# macOS / BSD backend — kqueue (event-driven, zero idle CPU)
# ---------------------------------------------------------------------------


class KqueueWatcher(threading.Thread):
    """
    Uses BSD kqueue to watch each file for VNODE events.

    kqueue watches open file descriptors — when a watched file is replaced
    atomically by an editor (write to tmp + rename), the original fd receives
    a KQ_NOTE_DELETE or KQ_NOTE_RENAME event.  We detect that, re-open the
    new inode, and register it again.
    """

    def __init__(self, paths: list[Path], debouncer: Debouncer, repo: Path) -> None:
        super().__init__(daemon=True, name="kqueue-watcher")
        self._paths = paths
        self._debouncer = debouncer
        self._repo = repo
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        import select as _select

        KQ_FILTER_VNODE = _select.KQ_FILTER_VNODE
        KQ_EV_ADD = _select.KQ_EV_ADD
        KQ_EV_CLEAR = _select.KQ_EV_CLEAR
        KQ_EV_EOF = _select.KQ_EV_EOF
        KQ_NOTE_WRITE = _select.KQ_NOTE_WRITE
        KQ_NOTE_ATTRIB = _select.KQ_NOTE_ATTRIB
        KQ_NOTE_RENAME = _select.KQ_NOTE_RENAME
        KQ_NOTE_DELETE = _select.KQ_NOTE_DELETE

        WATCH_FLAGS = KQ_NOTE_WRITE | KQ_NOTE_ATTRIB | KQ_NOTE_RENAME | KQ_NOTE_DELETE
        ADD_FLAGS = KQ_EV_ADD | KQ_EV_CLEAR

        kq = _select.kqueue()

        # fd -> Path mapping
        fd_to_path: dict[int, Path] = {}

        def _open_fd(path: Path) -> int | None:
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
                return fd
            except OSError:
                return None

        def _register(path: Path) -> None:
            fd = _open_fd(path)
            if fd is None:
                return
            ev = _select.kevent(
                fd,
                filter=KQ_FILTER_VNODE,
                flags=ADD_FLAGS,
                fflags=WATCH_FLAGS,
            )
            try:
                kq.control([ev], 0)
                fd_to_path[fd] = path
            except OSError:
                os.close(fd)

        def _notify(path: Path) -> None:
            self._debouncer.notify(rel_key(self._repo, path))

        # Initial registration of all paths that exist.
        for p in self._paths:
            if p.exists():
                _register(p)

        # Also keep a set of not-yet-existing paths so we can poll for them
        # appearing (editors sometimes create a file that didn't exist before).
        missing: set[Path] = {p for p in self._paths if not p.exists()}

        try:
            while not self._stop_event.is_set():
                # Poll for events with a 0.5s timeout so we can check
                # self._stop_event and also detect new files appearing.
                try:
                    events = kq.control(None, 32, 0.5)
                except OSError:
                    break

                # Check for newly-created files (e.g. devtools/__main__.py didn't
                # exist yet when the watcher started).
                for p in list(missing):
                    if p.exists():
                        missing.discard(p)
                        _register(p)
                        log_info(f"Now watching {_cyan(p.name)} (newly created)")

                for ev in events:
                    fd = ev.ident
                    path = fd_to_path.get(fd)
                    if path is None:
                        continue

                    fflags = ev.fflags

                    if fflags & (KQ_NOTE_DELETE | KQ_NOTE_RENAME):
                        # The inode was replaced — close old fd, re-open new one.
                        os.close(fd)
                        del fd_to_path[fd]
                        # Brief pause for the rename to settle.
                        time.sleep(0.05)
                        if path.exists():
                            _register(path)
                        else:
                            missing.add(path)

                    if fflags & (
                        KQ_NOTE_WRITE | KQ_NOTE_ATTRIB | KQ_NOTE_DELETE | KQ_NOTE_RENAME
                    ):
                        _notify(path)

        finally:
            for fd in list(fd_to_path):
                try:
                    os.close(fd)
                except OSError:
                    pass
            kq.close()


# ---------------------------------------------------------------------------
# Linux backend — inotify via ctypes
# ---------------------------------------------------------------------------

# inotify event flags
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MODIFY = 0x00000002
_IN_MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE | _IN_MODIFY


def _try_inotify(
    paths: list[Path], debouncer: Debouncer, repo: Path
) -> threading.Thread | None:
    """
    Attempt to set up an inotify watcher.  Returns a Thread on success, None
    if inotify is unavailable (no ctypes, no _ctypes extension, etc.).
    """
    try:
        import ctypes
        import ctypes.util
        import struct

        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            return None
        libc = ctypes.CDLL(libc_name, use_errno=True)
    except Exception:
        return None

    # Verify the inotify syscalls exist in this libc.
    try:
        libc.inotify_init1
        libc.inotify_add_watch
        libc.inotify_rm_watch
    except AttributeError:
        return None

    class InotifyWatcher(threading.Thread):
        # inotify_event struct: wd(i32) mask(u32) cookie(u32) len(u32) + name[len]
        _EVENT_HEADER = struct.Struct("iIII")
        _HEADER_SIZE = _EVENT_HEADER.size  # 16

        def __init__(self) -> None:
            super().__init__(daemon=True, name="inotify-watcher")
            self._stop_r, self._stop_w = os.pipe()
            self._ifd = -1
            self._repo = repo

        def stop(self) -> None:
            try:
                os.write(self._stop_w, b"\x00")
            except OSError:
                pass

        def run(self) -> None:
            import select as _select

            ifd = libc.inotify_init1(os.O_NONBLOCK)
            if ifd < 0:
                log_warn("inotify_init1 failed — falling back to polling")
                return
            self._ifd = ifd

            # Watch parent directories instead of individual files.
            # inotify on individual files doesn't survive atomic rename-writes.
            # dir_to_paths maps wd -> {basename: absolute_path}
            dir_to_paths: dict[int, dict[str, Path]] = {}  # wd -> {name: Path}
            watched_dirs: dict[Path, int] = {}  # dir -> wd

            for p in paths:
                d = p.parent
                if d not in watched_dirs:
                    wd = libc.inotify_add_watch(ifd, str(d).encode(), _IN_MASK)
                    if wd < 0:
                        continue
                    watched_dirs[d] = wd
                    dir_to_paths[wd] = {}
                wd = watched_dirs[d]
                dir_to_paths[wd][p.name] = p

            try:
                while True:
                    rlist, _, _ = _select.select([ifd, self._stop_r], [], [], 1.0)
                    if self._stop_r in rlist:
                        break
                    if ifd not in rlist:
                        continue

                    raw = os.read(ifd, 65536)
                    offset = 0
                    while offset < len(raw):
                        if offset + self._HEADER_SIZE > len(raw):
                            break
                        wd, mask, cookie, name_len = self._EVENT_HEADER.unpack_from(
                            raw, offset
                        )
                        offset += self._HEADER_SIZE
                        name_bytes = raw[offset : offset + name_len]
                        offset += name_len
                        name = name_bytes.rstrip(b"\x00").decode(errors="replace")

                        files = dir_to_paths.get(wd, {})
                        if name in files:
                            debouncer.notify(rel_key(self._repo, files[name]))
            finally:
                os.close(ifd)
                os.close(self._stop_r)
                os.close(self._stop_w)

    t = InotifyWatcher()
    return t


# ---------------------------------------------------------------------------
# Windows backend — ReadDirectoryChangesW via ctypes
# ---------------------------------------------------------------------------


def _try_readdir_changes(
    paths: list[Path], debouncer: Debouncer, repo: Path
) -> threading.Thread | None:
    """
    Use ReadDirectoryChangesW to watch the repo directory on Windows.
    Returns a Thread on success, None if unavailable.
    """
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except Exception:
        return None

    # Build a map from basename -> absolute path so we can recover the rel key.
    name_to_path: dict[str, Path] = {p.name: p for p in paths}
    dirs = {p.parent for p in paths}
    # We watch the repo root directory only.
    if not dirs:
        return None
    watch_dir = next(iter(dirs))

    FILE_LIST_DIRECTORY = 0x0001
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
    FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
    FILE_NOTIFY_CHANGE_SIZE = 0x00000008

    WATCH_FILTER = (
        FILE_NOTIFY_CHANGE_LAST_WRITE
        | FILE_NOTIFY_CHANGE_FILE_NAME
        | FILE_NOTIFY_CHANGE_SIZE
    )

    class RDCWatcher(threading.Thread):
        def __init__(self) -> None:
            super().__init__(daemon=True, name="rdcw-watcher")
            self._stop = threading.Event()
            self._repo = repo

        def stop(self) -> None:
            self._stop.set()

        def run(self) -> None:
            handle = kernel32.CreateFileW(
                str(watch_dir),
                FILE_LIST_DIRECTORY,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            if handle == ctypes.wintypes.HANDLE(-1).value:
                log_warn("CreateFileW failed — falling back to polling")
                return

            buf = ctypes.create_string_buffer(65536)
            bytes_returned = ctypes.wintypes.DWORD(0)

            try:
                while not self._stop.is_set():
                    ok = kernel32.ReadDirectoryChangesW(
                        handle,
                        buf,
                        len(buf),
                        False,  # not recursive — only repo root
                        WATCH_FILTER,
                        ctypes.byref(bytes_returned),
                        None,
                        None,
                    )
                    if not ok:
                        break
                    if bytes_returned.value == 0:
                        continue

                    # Parse FILE_NOTIFY_INFORMATION records.
                    offset = 0
                    raw = buf.raw[: bytes_returned.value]
                    while offset < len(raw):
                        next_offset = int.from_bytes(raw[offset : offset + 4], "little")
                        # action = int.from_bytes(raw[offset+4:offset+8], "little")
                        name_len = int.from_bytes(
                            raw[offset + 8 : offset + 12], "little"
                        )
                        name_raw = raw[offset + 12 : offset + 12 + name_len]
                        name = name_raw.decode("utf-16-le", errors="replace")
                        if name in name_to_path:
                            debouncer.notify(rel_key(self._repo, name_to_path[name]))
                        if next_offset == 0:
                            break
                        offset += next_offset
            finally:
                kernel32.CloseHandle(handle)

    return RDCWatcher()


# ---------------------------------------------------------------------------
# Fallback backend — pure stat-polling
# ---------------------------------------------------------------------------


class PollingWatcher(threading.Thread):
    """
    Watches files by polling their (mtime_ns, size) every `interval` seconds.
    Works on every platform with zero native dependencies.
    """

    def __init__(
        self,
        paths: list[Path],
        debouncer: Debouncer,
        repo: Path,
        interval: float = 0.3,
    ) -> None:
        super().__init__(daemon=True, name="poll-watcher")
        self._paths = paths
        self._debouncer = debouncer
        self._repo = repo
        self._interval = interval
        self._stop = threading.Event()
        # Initial fingerprints keyed by relative repo path (ACTION_MAP key).
        self._state: dict[str, tuple[int, int] | None] = {
            rel_key(repo, p): fingerprint(p) for p in paths
        }

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            for p in self._paths:
                key = rel_key(self._repo, p)
                new_fp = fingerprint(p)
                old_fp = self._state.get(key)
                if new_fp != old_fp:
                    self._state[key] = new_fp
                    self._debouncer.notify(key)


# ---------------------------------------------------------------------------
# Backend selector
# ---------------------------------------------------------------------------


def build_watcher(
    paths: list[Path],
    debouncer: Debouncer,
    repo: Path,
    force_poll: bool = False,
    poll_interval: float = 0.3,
) -> threading.Thread:
    """
    Choose and construct the best available file-watching backend.
    """
    if not force_poll:
        if (IS_MACOS or IS_BSD) and hasattr(__import__("select"), "kqueue"):
            log_info(f"Using {_bold('kqueue')} backend (macOS/BSD, event-driven)")
            return KqueueWatcher(paths, debouncer, repo)

        if IS_LINUX:
            watcher = _try_inotify(paths, debouncer, repo)
            if watcher is not None:
                log_info(f"Using {_bold('inotify')} backend (Linux, event-driven)")
                return watcher

        if IS_WINDOWS:
            watcher = _try_readdir_changes(paths, debouncer, repo)
            if watcher is not None:
                log_info(
                    f"Using {_bold('ReadDirectoryChangesW')} backend (Windows, event-driven)"
                )
                return watcher

    log_info(
        f"Using {_bold('stat-polling')} backend (interval: {poll_interval}s)"
        + (" (--poll)" if force_poll else " (native backend unavailable)")
    )
    return PollingWatcher(paths, debouncer, repo, interval=poll_interval)


# ---------------------------------------------------------------------------
# Re-exec under system Python if we're running inside Cosmopolitan Python
# ---------------------------------------------------------------------------


def _is_cosmo_python() -> bool:
    """
    Detect whether we're running inside the bundled Cosmopolitan Python,
    which lacks ctypes/_ctypes and native select extensions.
    """
    exe = sys.executable or ""
    # Cosmo Python reports itself via the exe path ending in .com or pyz
    # or has no real sys.prefix on the filesystem.
    if exe.endswith(".com") or exe.endswith(".pyz"):
        return True
    try:
        import ctypes  # noqa: F401

        ctypes.CDLL  # noqa: B018
    except (ImportError, AttributeError):
        return True
    import select as _sel

    # Cosmopolitan Python has poll but not kqueue/epoll on macOS — if we're
    # on macOS and kqueue is absent, we're almost certainly in Cosmo Python.
    if IS_MACOS and not hasattr(_sel, "kqueue"):
        return True
    return False


def _find_system_python() -> str | None:
    """Find the best system Python >= 3.10 to use for the watcher."""
    candidates = [
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
        "python",
    ]
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run(
                [exe, "-c", "import sys; v=sys.version_info; print(v[0]*100+v[1])"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                ver = int(r.stdout.strip())
                if ver >= 310:
                    return exe
        except Exception:
            continue
    return None


def _reexec_under_system_python(system_python: str) -> None:
    """Replace the current process with the same script under system Python."""
    script = __file__
    # If __file__ is inside a zip (the APE case), extract ourselves to a
    # temp file so system Python can exec us as a plain .py file.
    if ".com" in script or ".pyz" in script or not Path(script).exists():
        import tempfile
        import zipimport

        try:
            zi = zipimport.zipimporter(Path(script).parent)
            source = zi.get_source("__main__")
        except Exception:
            # Last resort: read via __loader__
            source = None
            if hasattr(__loader__, "get_source"):
                source = __loader__.get_source("__main__")  # type: ignore[name-defined]
        if source:
            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix="_developer_daemon.py",
                delete=False,
                prefix="scriptoscope_",
            )
            tmp.write(source)
            tmp.flush()
            tmp.close()
            script = tmp.name

    os.execv(system_python, [system_python, script] + sys.argv[1:])
    # execv replaces this process — we never reach here.


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="developer-daemon.com",
        description="ScriptoScope developer daemon — auto-rebuild on file changes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Watched files and the actions they trigger:

              scriptoscope.py          →  pipx install . --force
              pyproject.toml           →  pipx install . --force
                                          pipx inject scriptoscope -r requirements.txt --force
              requirements.txt         →  pipx install . --force
                                          pipx inject scriptoscope -r requirements.txt --force
              devtools/__main__.py     →  sh devtools/build-ape.sh
              devtools/watch.py        →  sh devtools/build-ape.sh
              devtools/build-ape.sh    →  sh devtools/build-ape.sh

            When multiple watched files change in the same debounce window,
            the highest-priority action is run once (rebuild-APE > reinstall+deps > reinstall).

            Examples:
              ./developer-daemon.com
              ./developer-daemon.com /path/to/ScriptoScope
              ./developer-daemon.com --poll --interval 0.5
              ./developer-daemon.com --no-tests
              sh developer-daemon.com   # if direct execute fails on some Linux configs
        """),
    )
    p.add_argument(
        "repo",
        nargs="?",
        default=None,
        metavar="REPO_PATH",
        help="Path to the ScriptoScope repository root (default: auto-detect)",
    )
    p.add_argument(
        "--poll",
        action="store_true",
        help="Force stat-polling even when a native backend is available",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=0.3,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 0.3, only used with --poll)",
    )
    p.add_argument(
        "--no-tests",
        action="store_true",
        help="Pass --no-tests to any setup-dev-env.com invocations (not used by watcher actions directly)",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational output",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------


def find_repo_root(hint: str | None) -> Path:
    """Return the repo root directory.

    The binary lives at <repo>/developer-daemon.com but watch.py is unpacked
    from inside the ZIP to a temp path, so we walk upward from __file__ until
    we find pyproject.toml.  When watch.py lives at devtools/watch.py the walk
    correctly stops at the repo root (one level above devtools/).
    """
    if hint:
        p = Path(hint).resolve()
        if not p.is_dir():
            print(f"error: {hint!r} is not a directory", file=sys.stderr)
            sys.exit(1)
        return p

    # Walk up from __file__ looking for pyproject.toml.
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate

    # Try CWD.
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists():
        return cwd

    print(
        "error: cannot locate the ScriptoScope repository root.\n"
        "Run developer-daemon.com from inside the cloned repository, or pass REPO_PATH.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


def print_banner(repo: Path, paths: list[Path], backend: str) -> None:
    width = 72
    bar = "━" * width
    print(f"\n{_bold(_cyan(bar))}")
    print(f"  {_bold('ScriptoScope Developer Daemon')}  {_dim(f'v{WATCHER_VERSION}')}")
    print(f"  {_dim('Ctrl-C to stop')}")
    print(f"{_bold(_cyan(bar))}")
    print(f"  {_bold('Repo')}     : {repo}")
    print(f"  {_bold('Backend')} : {backend}")
    print(f"  {_bold('Debounce')}: {DEBOUNCE_SECONDS}s")
    print(f"\n  {_bold('Watching:')}")
    for p in paths:
        key = rel_key(repo, p)
        exists_marker = "" if p.exists() else f"  {_dim('(will watch when created)')}"
        action = ACTION_MAP.get(key)
        action_name = f"  → {_magenta(action.name)}" if action else ""
        print(f"    {_cyan(key)}{action_name}{exists_marker}")
    print(f"\n{_bold(_cyan(bar))}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global _COLOR

    args = parse_args()

    # Colour setup.
    if args.no_color or not sys.stdout.isatty():
        _COLOR = False

    # ── Re-exec under system Python if needed ──────────────────────────────
    if _is_cosmo_python():
        system_python = _find_system_python()
        if system_python is None:
            print(
                "error: developer-daemon.com requires a system Python >= 3.10 on PATH.\n"
                "Run setup-dev-env.com first to install one.",
                file=sys.stderr,
            )
            sys.exit(1)
        log_info(f"Re-execing under system Python: {_cyan(system_python)}")
        _reexec_under_system_python(system_python)
        sys.exit(1)  # unreachable

    # ── Locate repo root ───────────────────────────────────────────────────
    repo = find_repo_root(args.repo)

    # ── Resolve watched paths ──────────────────────────────────────────────
    watched_paths = [repo / name for name in WATCHED_FILES]

    # ── Verify pipx is available (required for reinstall actions) ─────────
    if not shutil.which("pipx"):
        log_warn(
            "pipx not found on PATH — reinstall actions will fail.\n"
            "  Run setup-dev-env.com first, then open a new shell and try again."
        )

    # ── Build the component stack ──────────────────────────────────────────
    runner = ActionRunner(args)
    debouncer = Debouncer(runner, repo, args)

    # Determine which backend we'll use (for the banner).
    if args.poll:
        backend_name = f"stat-polling ({args.interval}s)"
    elif IS_MACOS or IS_BSD:
        backend_name = "kqueue"
    elif IS_LINUX:
        # Check if inotify will work.
        try:
            import ctypes

            backend_name = "inotify"
        except ImportError:
            backend_name = f"stat-polling ({args.interval}s)"
    elif IS_WINDOWS:
        backend_name = "ReadDirectoryChangesW"
    else:
        backend_name = f"stat-polling ({args.interval}s)"

    print_banner(repo, watched_paths, backend_name)

    watcher = build_watcher(
        watched_paths,
        debouncer,
        repo,
        force_poll=args.poll,
        poll_interval=args.interval,
    )

    # ── Signal handling ────────────────────────────────────────────────────
    def _shutdown(sig: int, frame: object) -> None:
        print(f"\n\n{_dim('Received signal — shutting down watcher…')}")
        watcher.stop()
        runner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Start threads ──────────────────────────────────────────────────────
    runner.start()
    watcher.start()

    log_info(
        f"Developer daemon active. Watching {len(watched_paths)} files in {_cyan(str(repo))}"
    )

    # ── Main thread: keep alive, restart watcher thread if it dies ─────────
    while True:
        watcher.join(timeout=5.0)
        if not watcher.is_alive():
            # Watcher thread crashed — restart it.
            log_warn("Daemon watcher thread exited unexpectedly — restarting…")
            watcher = build_watcher(
                watched_paths,
                debouncer,
                repo,
                force_poll=args.poll,
                poll_interval=args.interval,
            )
            watcher.start()


if __name__ == "__main__":
    main()
