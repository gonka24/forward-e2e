"""Global runtime lock abstraction for the suite runtime.

Uses standard Linux flock. Production callers always pass an explicit path
(``<workspace>/exclusive.lock`` from the supervisor, ``<runtime_root>/exclusive.lock``
from the orchestrator); ``~/forward-e2e-runtime/exclusive.lock`` is only the fallback
for a process without a configured workspace, mirroring
``get_default_runtime_root`` in ``runtime.py``. Provides fail-fast semantics
on contention without deleting lock files.
Supports re-entrant / shared usage within the same orchestrator process to
prevent self-deadlock when calling underlying runtime actions.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from typing import Optional


class LockContentionError(RuntimeError):
    """Raised when the exclusive runtime lock cannot be acquired immediately."""

    code = "RUNTIME_LOCK_BUSY"
    exit_code = 1


class LockOperationError(RuntimeError):
    """The operating system could not perform the locking operation."""

    code = "RUNTIME_LOCK_FAILED"
    exit_code = 1


class RuntimeLock:
    """Non-blocking exclusive lock for suite runtime operations.

    Ensures that only one process at a time can prepare, run, or clean up
    the ``~/forward-e2e-runtime`` environment.
    """

    def __init__(self, lock_path: Optional[Path] = None):
        if lock_path is None:
            home = Path.home()
            self.lock_path = home / "forward-e2e-runtime" / "exclusive.lock"
        else:
            self.lock_path = Path(lock_path).resolve()
        self._fd: Optional[int] = None
        self._lock_count: int = 0  # Support nested acquires in the same process

    @property
    def is_acquired(self) -> bool:
        return self._lock_count > 0 and self._fd is not None

    def acquire(self) -> None:
        """Acquire the lock exclusively in non-blocking mode.

        If already acquired by this instance, increments the re-entrancy counter.
        If locked by another process, raises LockContentionError immediately.
        """
        if self._lock_count > 0:
            self._lock_count += 1
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append/write mode without truncating
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise LockOperationError(f"Cannot acquire runtime lock {self.lock_path}: {exc}") from exc
            raise LockContentionError(
                f"Runtime lock {self.lock_path} is busy. Another suite run or launcher is active. "
                "Do NOT delete this lock file to force release."
            ) from exc

        self._fd = fd
        self._lock_count = 1

    def release(self) -> None:
        """Release the lock. Decrements re-entrancy counter.

        Releases the actual OS lock only when the outermost acquire exits.
        Never unlinks the lock file.
        """
        if self._lock_count <= 0:
            return

        self._lock_count -= 1
        if self._lock_count > 0:
            return

        if self._fd is not None:
            fd = self._fd
            self._fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            finally:
                os.close(fd)

    def __enter__(self) -> RuntimeLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
