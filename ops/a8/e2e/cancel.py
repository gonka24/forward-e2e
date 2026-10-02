"""Stopping a run when the operator asks, including mid-build.

A long E2E build spends most of its wall time inside ``make``, ``docker build``
and Gradle, all of which are children of this process. Recording "a signal
arrived" is therefore not enough: without doing anything about it, Ctrl-C only
takes effect once the multi-hour build has finished on its own, and the exit
code is decided long after the operator gave up.

This module makes cancellation actually cancel:

* :class:`Cancellation` is a token. The signal handler calls :meth:`request`;
  everything else asks :meth:`raise_if_requested` at stage boundaries.
* Every build subprocess is started in its **own process group** and registered
  with the token, so the whole tree -- ``make``, the compilers it spawns, the
  ``docker`` CLI -- can be signalled, not just the direct child. Killing only
  the direct child would orphan the real workers.
* Termination is graceful first (``SIGTERM`` to the group), then forceful
  (``SIGKILL``) after a grace period, so a build that traps signals cannot hold
  the runner hostage.
* Nothing here deletes evidence. The caller unwinds through the normal failure
  path, which writes the partial build manifest and the execution manifest.

Signals are only *installed* by the CLI; this module never touches the signal
disposition on import, so tests can drive it directly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from .errors import ExecutionCancelled

#: Seconds between the polite SIGTERM and the final SIGKILL of a build group.
DEFAULT_GRACE_SECONDS = 15.0

#: The same policy for a build that blew its own timeout. It is deliberately
#: shorter than the cancellation grace: that build has already been given every
#: second its recipe asked for, and nobody is waiting to answer a prompt.
TIMEOUT_GRACE_SECONDS = 2.0

# A detached descendant may keep inherited output pipes open after group stop.
OUTPUT_DRAIN_SECONDS = 2.0

#: POSIX convention: 128 + signal number.
_EXIT_CODES = {signal.SIGINT: 130, signal.SIGTERM: 143}


class Cancellation:
    """A one-way switch from "running" to "stopping", shared by all stages."""

    def __init__(self, *, grace_seconds: float = DEFAULT_GRACE_SECONDS):
        self._lock = threading.RLock()
        self._signum: Optional[int] = None
        self._requested_at: Optional[float] = None
        self._groups: Set[int] = set()
        self._escalations: Dict[int, threading.Thread] = {}
        self._grace_seconds = float(grace_seconds)
        self._signalled_groups: List[Dict[str, Any]] = []

    # -- state ---------------------------------------------------------
    @property
    def requested(self) -> bool:
        with self._lock:
            return self._signum is not None

    @property
    def signum(self) -> Optional[int]:
        with self._lock:
            return self._signum

    @property
    def grace_seconds(self) -> float:
        """How long a build group is given to stop politely before SIGKILL."""
        return self._grace_seconds

    @property
    def exit_code(self) -> int:
        with self._lock:
            if self._signum is None:
                return 0
            return _EXIT_CODES.get(self._signum, 128 + int(self._signum))

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "requested": self._signum is not None,
                "signal": int(self._signum) if self._signum is not None else None,
                "exit_code": self.exit_code,
                "requested_at_epoch": self._requested_at,
                "signalled_process_groups": list(self._signalled_groups),
            }

    # -- process groups ------------------------------------------------
    def register_process_group(self, pgid: int) -> None:
        """Track a build's process group so it can be stopped as a unit."""
        with self._lock:
            self._groups.add(int(pgid))
            already = self._signum is not None
            if already:
                # Publish the escalation before another thread can finish the
                # group, including when cancellation preceded registration.
                self._terminate_group(int(pgid))

    def unregister_process_group(self, pgid: int) -> None:
        with self._lock:
            self._groups.discard(int(pgid))
            self._escalations.pop(int(pgid), None)

    def finish_process_group(self, pgid: int) -> None:
        """Finish a pending cancellation before releasing group ownership.

        Pipe EOF and a reaped leader do not imply that its descendants exited.
        Keep ownership until the escalation worker has checked the group and,
        if necessary, sent SIGKILL. Normal completion remains immediate.
        """
        with self._lock:
            escalation = self._escalations.get(int(pgid))
            if escalation is None:
                self.unregister_process_group(pgid)
                return
        escalation.join()
        self.unregister_process_group(pgid)

    # -- the switch ----------------------------------------------------
    def request(self, signum: int, *, emit: Optional[Callable[[str], None]] = None) -> None:
        """Record the signal and stop every running build immediately."""
        say = emit or (lambda message: None)
        with self._lock:
            first = self._signum is None
            if first:
                self._signum = int(signum)
                self._requested_at = time.time()
            groups = sorted(self._groups)
            for pgid in groups:
                if first:
                    self._terminate_group(pgid)
                else:
                    self._kill_group(pgid)
        if not first:
            # A second Ctrl-C: the operator is impatient. Escalate at once.
            say("Second termination signal: killing the build processes immediately.")
            return
        say(
            "Received a termination signal. Stopping the running build and preserving the "
            "evidence produced so far; no result is reported as passed."
        )

    def raise_if_requested(self, stage: str) -> None:
        """Stop between stages. Called where partial state is consistent."""
        with self._lock:
            signum = self._signum
        if signum is None:
            return
        raise ExecutionCancelled(
            "The run was cancelled by a termination signal. Nothing after this point was "
            "executed, and the partial evidence is kept exactly as it was.",
            {"stage": stage, "signal": int(signum)},
            exit_code=self.exit_code,
        )

    # -- signalling ----------------------------------------------------
    def _terminate_group(self, pgid: int) -> None:
        """SIGTERM now, SIGKILL later, without blocking the signal handler.

        ``request`` runs inside a signal handler on the main thread, which is
        exactly the thread that is waiting for the build. Sleeping there would
        stall the very cleanup it is trying to perform, so the escalation waits
        on a daemon thread instead.
        """
        self._signal_group(pgid, signal.SIGTERM, "terminate")
        escalation = threading.Thread(
            target=self._escalate_after_grace,
            args=(int(pgid),),
            name=f"a8-cancel-{pgid}",
            daemon=True,
        )
        self._escalations[int(pgid)] = escalation
        escalation.start()

    def _escalate_after_grace(self, pgid: int) -> None:
        deadline = time.monotonic() + self._grace_seconds
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        with self._lock:
            still_ours = int(pgid) in self._groups
            if not still_ours:
                # After ownership is released the kernel may recycle this id;
                # signalling it could then hit an unrelated process.
                return
            self._kill_group(pgid)

    def _kill_group(self, pgid: int) -> None:
        self._signal_group(pgid, signal.SIGKILL, "kill")

    def _signal_group(self, pgid: int, sig: int, action: str) -> None:
        record = {"pgid": int(pgid), "action": action, "at_epoch": time.time()}
        try:
            os.killpg(int(pgid), sig)
        except OSError as exc:
            record["error"] = str(exc)
        with self._lock:
            self._signalled_groups.append(record)


def _group_alive(pgid: int) -> bool:
    """Only a definite "no such group" counts as gone.

    A permission error means the group exists but is not ours to signal, and
    treating that as death would skip the SIGKILL escalation for a build that
    is still running.
    """
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def make_build_runner(
    cancellation: Optional[Cancellation] = None,
    *,
    timeout_grace_seconds: float = TIMEOUT_GRACE_SECONDS,
) -> Callable[..., subprocess.CompletedProcess]:
    """A build runner whose children can be stopped as a group.

    Same signature and same return type as the builder's default runner, so it
    is a drop-in replacement; the only difference is that the child leads its
    own session and is registered with the token for the duration of the call.
    """

    def run(
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        if cancellation is not None:
            cancellation.raise_if_requested("build-step-start")
        process = subprocess.Popen(  # noqa: S603 - argv is built from the recipe
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pgid = _process_group_of(process)
        if cancellation is not None and pgid is not None:
            cancellation.register_process_group(pgid)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop(process, pgid, grace_seconds=timeout_grace_seconds)
            try:
                stdout, stderr = process.communicate(timeout=OUTPUT_DRAIN_SECONDS)
            except subprocess.TimeoutExpired as drain:
                # communicate's timeout snapshot is cumulative. Do not append
                # it to the first snapshot, which would duplicate the log.
                stdout = drain.output if drain.output is not None else exc.output
                stderr = drain.stderr if drain.stderr is not None else exc.stderr
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                # Reap the leader when possible without waiting for detached
                # writers. Preserve the original command timeout either way.
                try:
                    process.wait(timeout=OUTPUT_DRAIN_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            # Include output flushed during shutdown in the builder's timeout
            # log, rather than retaining only the initial timeout's snapshot.
            exc.output = stdout
            exc.stderr = stderr
            raise
        finally:
            if cancellation is not None and pgid is not None:
                cancellation.finish_process_group(pgid)
        # The result is returned even when the child was killed by the
        # cancellation, so the caller can still write its log and record the
        # command before unwinding. Deciding that the run is over is the
        # caller's job, not this runner's.
        return subprocess.CompletedProcess(
            list(argv), process.returncode, stdout or "", stderr or ""
        )

    return run


def _process_group_of(process: subprocess.Popen) -> Optional[int]:
    try:
        return os.getpgid(process.pid)
    except OSError:
        return None


def _stop(
    process: subprocess.Popen,
    pgid: Optional[int],
    *,
    grace_seconds: float = TIMEOUT_GRACE_SECONDS,
) -> None:
    """Ask the build group to stop, then insist. Same policy as cancellation."""
    if pgid is None:
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        tick_end = min(deadline, time.monotonic() + 0.1)
        # Reap our own child first. An exited-but-uncollected child is a
        # zombie, and a zombie is still a member of its process group, so
        # `_group_alive` would report the whole group as running until
        # `communicate()` collects it -- which happens only after this function
        # returns. Without this, a build that obeyed SIGTERM immediately still
        # burnt the entire grace period and was then SIGKILLed as a corpse.
        try:
            process.wait(timeout=max(0.0, tick_end - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        if not _group_alive(pgid):
            return
        # wait() is instantaneous after the leader exits, even while other
        # group members remain. Pace those checks rather than busy-spinning.
        time.sleep(max(0.0, tick_end - time.monotonic()))
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


__all__ = [
    "Cancellation",
    "DEFAULT_GRACE_SECONDS",
    "TIMEOUT_GRACE_SECONDS",
    "make_build_runner",
]
