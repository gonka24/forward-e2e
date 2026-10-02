"""DinD supervisor for private inner Docker daemon lifecycle.

Invariant:
  - Host Docker socket is NEVER mounted or used.
  - dockerd is started ONLY for executions under an exclusive lock.
  - Global flock is acquired BEFORE dockerd starts and held until dockerd terminates.
  - A second container attempting execution fails fast without corrupting shared data-root.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Optional

from .lock import RuntimeLock
from .runtime import RuntimeErrorA8


class DinDSupervisor:
    """Manages the private inner Docker daemon lifecycle with bounded readiness and strict cleanup."""

    def __init__(
        self,
        workspace_dir: Path,
        data_root: Optional[Path] = None,
        socket_path: Optional[Path] = None,
        pid_file: Optional[Path] = None,
        log_file: Optional[Path] = None,
    ):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.data_root = Path(data_root).resolve() if data_root else Path("/var/lib/docker")
        self.socket_path = Path(socket_path).resolve() if socket_path else Path("/var/run/docker.sock")
        self.pid_file = Path(pid_file).resolve() if pid_file else Path("/var/run/docker.pid")
        self.log_file = Path(log_file).resolve() if log_file else (self.workspace_dir / "dockerd.log")
        self.lock = RuntimeLock(self.workspace_dir / "exclusive.lock")
        self.proc: Optional[subprocess.Popen] = None
        self._owns_dockerd: bool = False

    def is_daemon_ready(self, timeout: float = 2.0) -> bool:
        """Probe Docker daemon via standard docker info with private socket."""
        env = os.environ.copy()
        env["DOCKER_HOST"] = f"unix://{self.socket_path}"
        try:
            res = subprocess.run(
                ["docker", "info"],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return res.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def start_dockerd(self, timeout_seconds: float = 60.0, allow_existing: bool = False) -> None:
        """Acquires lock, starts private dockerd, and waits until responsive."""
        # 1. Acquire persistent flock BEFORE touching dockerd or shared data-root
        self.lock.acquire()
        try:
            self._start_dockerd_locked(timeout_seconds, allow_existing)
        except Exception:
            # Directory preparation, socket probing and log opening can all
            # fail before Popen. No daemon owns the storage in that case, so
            # release this acquisition; keep it if a launched daemon survives.
            if not self._owns_dockerd and self.lock.is_acquired:
                self.lock.release()
            raise

    def _start_dockerd_locked(self, timeout_seconds: float, allow_existing: bool) -> None:
        """Start under the already-held workspace lock."""

        # Check if already responsive (e.g. testing with pre-existing daemon)
        if self.is_daemon_ready(timeout=1.0):
            if not allow_existing:
                raise RuntimeErrorA8(
                    f"A Docker daemon is already responsive on private socket {self.socket_path}.\n"
                    "Refusing to use an unverified, pre-existing daemon on persistent storage.\n"
                    "Please ensure no other containers are using this workspace volume."
                )
            print("Detected already responsive Docker daemon on private socket.")
            return

        # Prepare directories and clean stale socket/pid
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        if self.pid_file.exists():
            try:
                self.pid_file.unlink()
            except OSError:
                pass

        cmd = [
            "dockerd",
            "--data-root", str(self.data_root),
            "--host", f"unix://{self.socket_path}",
            "--pidfile", str(self.pid_file),
        ]

        # Normalize Docker environment
        os.environ["DOCKER_HOST"] = f"unix://{self.socket_path}"
        os.environ.pop("DOCKER_CONTEXT", None)
        os.environ.pop("DOCKER_TLS_VERIFY", None)
        os.environ.pop("DOCKER_CERT_PATH", None)

        print(f"Starting inner dockerd (data-root={self.data_root}, socket={self.socket_path})...")
        log_f = open(self.log_file, "a", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._owns_dockerd = True
        except Exception as exc:
            log_f.close()
            raise RuntimeErrorA8(f"Failed to launch dockerd process: {exc}") from exc

        # Bounded readiness loop
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                log_f.close()
                code = self.proc.returncode
                self.stop_dockerd()
                log_tail = self._read_log_tail(lines=30)
                raise RuntimeErrorA8(
                    f"Inner dockerd process exited unexpectedly with code {code}.\n"
                    f"Last log output:\n{log_tail}"
                )

            if self.is_daemon_ready(timeout=1.5):
                print("Inner dockerd is ready and responsive.")
                log_f.close()
                return

            time.sleep(0.5)

        log_f.close()
        self.stop_dockerd()
        log_tail = self._read_log_tail(lines=30)
        raise RuntimeErrorA8(
            f"Inner dockerd failed to become responsive within {timeout_seconds}s.\n"
            f"Last log output:\n{log_tail}"
        )

    def stop_dockerd(self, timeout_seconds: float = 60.0) -> bool:
        """Gracefully terminates dockerd process and releases exclusive lock.
        Returns True if dockerd is confirmed stopped, False otherwise.
        """
        # Unexpected signal errors must not release storage still in use.
        stopped = False
        try:
            if self._owns_dockerd and self.proc and self.proc.poll() is None:
                print("Stopping inner dockerd...")
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(self.proc.pid, signal.SIGTERM)
                    else:
                        self.proc.terminate()
                except ProcessLookupError:
                    pass

                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.5)

                if self.proc.poll() is None:
                    print("dockerd did not terminate gracefully; escalating to SIGKILL.", file=sys.stderr)
                    try:
                        if hasattr(os, "killpg"):
                            os.killpg(self.proc.pid, signal.SIGKILL)
                        else:
                            self.proc.kill()
                        self.proc.wait(timeout=5.0)
                        stopped = True
                    except subprocess.TimeoutExpired:
                        stopped = False
                        print("Error: dockerd process could not be terminated after SIGKILL.", file=sys.stderr)
                    except Exception:
                        stopped = self.proc.poll() is not None
                else:
                    stopped = True
            else:
                stopped = True
        finally:
            # Keep the lock and ownership unless daemon termination is confirmed.
            if stopped:
                self.proc = None
                self._owns_dockerd = False
                if self.lock.is_acquired:
                    self.lock.release()
        return stopped

    def _read_log_tail(self, lines: int = 30) -> str:
        if not self.log_file.is_file():
            return "(no log file found)"
        try:
            content = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except Exception as exc:
            return f"(failed to read log tail: {exc})"


__all__ = ["DinDSupervisor"]
