"""Execution adapters for native Testermint and boundary tasks.

Handles:
- Process-tree lifecycle and confirmed descendant termination
- Real-time log streaming directly to disk
- Periodic heartbeat (at least every 60s)
- Accurate stage timeout budgets (Gradle 45m/75m vs outer runner budgets)
- Precise started.json marker with live_attempt=0 during prepare and live_attempt=1 only on live entry
- Consistent task_evidence_dir layout (<snapshot>/evidence/<run_id>/)
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .collector import copy_checked_runtime_file
from .models import ExecutionStatus, TaskPlan
from .runtime import RuntimeSnapshot


class ProcessExecutionError(RuntimeError):
    """A subprocess tree could not be confirmed terminated."""

    code = "PROCESS_TREE_TERMINATION_FAILED"
    exit_code = 1


class SubprocessRunner:
    """Manages execution of a long-running subprocess with streaming output and heartbeat."""

    def __init__(
        self,
        argv: Sequence[str],
        cwd: Path,
        log_file: Path,
        timeout_seconds: float,
        heartbeat_interval_seconds: float = 30.0,
        env: Optional[Mapping[str, str]] = None,
        heartbeat_callback: Optional[Callable[[str, float, float, Path], None]] = None,
        phase_getter: Optional[Callable[[], str]] = None,
    ):
        self.argv = list(argv)
        self.cwd = Path(cwd).resolve()
        self.log_file = Path(log_file).resolve()
        self.timeout_seconds = timeout_seconds
        self.heartbeat_interval_seconds = max(5.0, heartbeat_interval_seconds)
        self.env = dict(env) if env else os.environ.copy()
        self.heartbeat_callback = heartbeat_callback
        self.phase_getter = phase_getter or (lambda: "RUNNING")
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.exit_code: Optional[int] = None
        self.timed_out: bool = False
        self.cancelled: bool = False
        self.pgid: Optional[int] = None
        self._tree_terminated = False

    def terminate_own_tree(self) -> None:
        """Gracefully terminate own process group, then force kill, confirming no descendants linger."""
        if self.proc is None or self._tree_terminated:
            return

        pid = self.proc.pid
        pgid = self.pgid
        if pgid is None and hasattr(os, "getpgid"):
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                self._tree_terminated = True
                return

        # 1. Send SIGTERM to entire process group
        if pgid is not None and hasattr(os, "killpg"):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                self._tree_terminated = True
                return
        else:
            try:
                self.proc.terminate()
            except Exception:
                pass

        # 2. Wait up to 3 seconds for group to exit
        start_wait = time.monotonic()
        group_alive = True
        while time.monotonic() - start_wait < 3.0:
            self.proc.poll()  # Reap the leader so it cannot keep the group alive as a zombie.
            if pgid is not None and hasattr(os, "kill"):
                try:
                    os.kill(-pgid, 0)
                except ProcessLookupError:
                    group_alive = False
                    self._tree_terminated = True
                    break
            elif self.proc.poll() is not None:
                group_alive = False
                self._tree_terminated = True
                break
            time.sleep(0.2)

        # 3. If any processes still linger in group, send SIGKILL
        if group_alive:
            if pgid is not None and hasattr(os, "killpg"):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                try:
                    self.proc.kill()
                except Exception:
                    pass

            # Wait up to 3 seconds for SIGKILL to complete
            kill_wait = time.monotonic()
            while time.monotonic() - kill_wait < 3.0:
                self.proc.poll()
                if pgid is not None and hasattr(os, "kill"):
                    try:
                        os.kill(-pgid, 0)
                    except ProcessLookupError:
                        self._tree_terminated = True
                        break
                elif self.proc.poll() is not None:
                    self._tree_terminated = True
                    break
                time.sleep(0.2)

        if not self._tree_terminated:
            raise ProcessExecutionError(f"Process group {pgid or pid} survived SIGKILL cleanup deadline")

        # Final wait on parent
        try:
            self.proc.wait(timeout=2.0)
        except Exception:
            pass

    def run(self) -> int:
        """Run the command, stream output to log_file, and emit heartbeats."""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()

        with self.log_file.open("wb") as out_f:
            preexec = os.setsid if hasattr(os, "setsid") else None

            self.proc = subprocess.Popen(
                self.argv,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.env,
                preexec_fn=preexec,
            )

            if hasattr(os, "getpgid") and self.proc.pid:
                try:
                    self.pgid = os.getpgid(self.proc.pid)
                except Exception:
                    self.pgid = self.proc.pid

            assert self.proc.stdout is not None
            stdout_stream = self.proc.stdout

            def reader():
                try:
                    while True:
                        chunk = stdout_stream.read(4096)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        out_f.flush()
                except Exception:
                    pass

            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()

            last_heartbeat = time.monotonic()

            try:
                while True:
                    code = self.proc.poll()
                    if code is not None:
                        self.exit_code = code
                        break

                    now = time.monotonic()
                    elapsed = now - start_time
                    if elapsed >= self.timeout_seconds:
                        self.timed_out = True
                        self.terminate_own_tree()
                        self.exit_code = -1
                        break

                    if now - last_heartbeat >= self.heartbeat_interval_seconds:
                        phase = self.phase_getter()
                        if self.heartbeat_callback:
                            self.heartbeat_callback(phase, elapsed, self.timeout_seconds, self.log_file)
                        last_heartbeat = now

                    time.sleep(0.5)

            except KeyboardInterrupt:
                self.cancelled = True
                self.exit_code = 130
                raise

            finally:
                # A reaped group leader can still leave children holding stdout open.
                try:
                    self.terminate_own_tree()
                except ProcessExecutionError:
                    # Preserve cancellation as the primary outcome. The orchestrator
                    # retries cleanup and records its failure independently.
                    if not self.cancelled:
                        raise
                finally:
                    reader_thread.join(timeout=2.0)

        return self.exit_code if self.exit_code is not None else -1


class NativeTaskAdapter:
    """Adapter for executing a single focused Kotlin Testermint scenario."""

    def __init__(
        self,
        task: TaskPlan,
        snapshot: RuntimeSnapshot,
        heartbeat_callback: Optional[Callable[[str, float, float, Path], None]] = None,
        e2e_context: Optional[Any] = None,
    ):
        self.task = task
        self.snapshot = snapshot
        self.heartbeat_callback = heartbeat_callback
        self.current_phase = "PREPARING"
        self.active_runner: Optional[SubprocessRunner] = None
        # Optional ops.a8.e2e.context.E2ERunContext. Duck-typed on purpose: the
        # legacy package must not import the E2E package that builds on it.
        self.e2e_context = e2e_context

    def terminate_own_tree(self) -> None:
        if self.active_runner:
            self.active_runner.terminate_own_tree()

    def resolve_harness_script(self) -> Path:
        """Locate the acceptance harness.

        The harness always comes from the runner image, never from the contracts
        checkout under test: choosing a different contracts commit must never
        silently swap the code that judges the run.
        """
        override = getattr(self.e2e_context, "harness_script", None)
        if override:
            return Path(override)
        runner_root = Path(__file__).resolve().parent.parent.parent
        return runner_root / "scripts" / "a8_acceptance.py"

    def execute(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        """Executes the scenario via a8_acceptance.py run-live.

        Writes started.json with live_attempt=0 initially, updating to 1 upon live entry.
        """
        task_ev = self.snapshot.task_evidence_dir

        # 1. Write preliminary started marker (live_attempt=0)
        started_data = {
            "run_id": self.snapshot.run_id,
            "task_id": self.task.task_id,
            "scenario": self.task.scenario_selector,
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "live_attempt": 0,
            "timeout_minutes": self.task.timeout_minutes,
            "phase": "PREPARING",
        }
        self.snapshot.started_file.write_text(json.dumps(started_data, indent=2) + "\n", encoding="utf-8")
        self.current_phase = "HARNESS_INIT"

        # 2. Build command
        harness_script = self.resolve_harness_script()
        if not harness_script.exists():
            return ExecutionStatus.FAILED, 1, f"Missing harness script: {harness_script}"

        scenario_arg = self.task.scenario_selector

        # Pass snapshot.evidence_dir as evidence-dir; a8_acceptance appends run_id to form task_evidence_dir
        cmd = [
            sys.executable,
            str(harness_script),
            "run-live",
            "--marketplace-dir", str(self.snapshot.marketplace_dir),
            "--gonka-dir", str(self.snapshot.gonka_dir),
            "--evidence-dir", str(self.snapshot.evidence_dir),
            "--run-id", self.snapshot.run_id,
            "--scenario", scenario_arg,
            "--timeout-minutes", str(self.task.gradle_timeout_minutes),
        ]
        # The plan's version guards and measured runtime expectations. They
        # parameterise the harness' existing checks; they never remove them.
        if self.e2e_context is not None and hasattr(self.e2e_context, "harness_argv_extras"):
            cmd.extend(self.e2e_context.harness_argv_extras())

        # Use supervisor launcher.log to prevent colliding with harness testermint.log
        log_file = self.snapshot.launcher_log

        # 3. Mark live attempt as starting
        started_data["live_attempt"] = 1
        started_data["phase"] = "TESTERMINT_RUNNING"
        self.snapshot.started_file.write_text(json.dumps(started_data, indent=2) + "\n", encoding="utf-8")

        def get_phase() -> str:
            if task_ev.is_dir():
                if (task_ev / "package-c" / "results.json").exists():
                    return "PACKAGE_C_ACTIVE"
                if (task_ev / "live-context.json").exists():
                    return "TESTERMINT_ACTIVE"
            return self.current_phase

        self.active_runner = SubprocessRunner(
            argv=cmd,
            # ``a8_acceptance.py run-live`` invokes Cargo for the contracts
            # under test.  The runner-owned runtime directory only holds
            # evidence and has no Cargo.toml, so the process must start in the
            # isolated marketplace checkout.
            cwd=self.snapshot.marketplace_dir,
            log_file=log_file,
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_interval_seconds=30.0,
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=get_phase,
        )

        self.current_phase = "TESTERMINT_EXECUTING"
        try:
            code = self.active_runner.run()
        except KeyboardInterrupt:
            return ExecutionStatus.CANCELLED, 130, "Task cancelled by user (Ctrl+C)"

        # Collect JUnit XML into snapshot.junit_dir
        self._collect_junit()

        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, code, f"Stage timed out after {self.task.timeout_minutes}m"
        if self.active_runner.cancelled:
            return ExecutionStatus.CANCELLED, 130, "Execution cancelled"
        if code == 0:
            return ExecutionStatus.PASSED, code, None
        return ExecutionStatus.FAILED, code, f"Testermint scenario exited with code {code}"

    def _collect_junit(self) -> None:
        """Copy the external harness' JUnit XML into ``snapshot.junit_dir``.

        The marketplace scenarios run in the runner-owned external Gradle
        project, whose build directory lives in the harness work root, and the
        harness copies their reports into ``<task evidence>/testermint-junit``.
        The Gonka snapshot is read-only and never holds test results, so it is
        deliberately not searched: a report found there would come from a
        tree this run did not produce.
        """
        harness_junit_dir = self.snapshot.task_evidence_dir / "testermint-junit"
        if harness_junit_dir.is_dir() and not harness_junit_dir.is_symlink():
            for xml in sorted(harness_junit_dir.glob("TEST-*.xml")):
                if xml.is_symlink() or not xml.is_file():
                    continue
                dest = self.snapshot.junit_dir / xml.name
                copy_checked_runtime_file(self.snapshot.run_dir, xml, self.snapshot.run_dir, dest)


class BoundaryTaskAdapter:
    """Adapter for running isolated boundary checks without Testermint."""

    def __init__(
        self,
        task: TaskPlan,
        snapshot: RuntimeSnapshot,
        heartbeat_callback: Optional[Callable[[str, float, float, Path], None]] = None,
        e2e_context: Optional[Any] = None,
    ):
        self.task = task
        self.snapshot = snapshot
        self.heartbeat_callback = heartbeat_callback
        self.current_phase = "BOUNDARY_INIT"
        self.active_runner: Optional[SubprocessRunner] = None
        self.e2e_context = e2e_context

    def terminate_own_tree(self) -> None:
        if self.active_runner:
            self.active_runner.terminate_own_tree()

    @property
    def cargo_target_dir(self) -> Path:
        """Cargo output directory for boundary builds, outside both snapshots.

        The marketplace snapshot is an immutable source tree: Cargo's default
        ``target/`` inside it would add files to the very tree whose
        immutability the run proves. The snapshot's run directory is owned by
        this task and is a sibling of both checkouts.
        """
        return self.snapshot.run_dir / "cargo-target"

    def cargo_environment(self) -> dict:
        """Process environment for Cargo with ``CARGO_TARGET_DIR`` pinned outside the snapshot."""
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(self.cargo_target_dir)
        return env

    def resolve_script(self, relative: str) -> Path:
        """Resolve a harness-side probe script.

        Boundary probes belong to the runner, not to the contracts under test.
        They always come from the runner image, so that a target repository cannot
        supply the code that measures it.
        """
        harness = getattr(self.e2e_context, "harness_script", None)
        if harness:
            return Path(harness).parent / relative
        runner_root = Path(__file__).resolve().parent.parent.parent
        return runner_root / "scripts" / relative

    def execute(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        if self.task.task_id == "go-boundary":
            return self._run_go_boundary()
        elif self.task.task_id == "wasm-abi-boundary":
            return self._run_wasm_abi_boundary()
        elif self.task.task_id == "ct-network-unconfirmed":
            return self._run_ct_network_unconfirmed()
        elif self.task.task_id == "ct-claim-expiry":
            return self._run_ct_claim_expiry()
        elif self.task.task_id == "ct-package-c-policy":
            return self._run_ct_package_c_policy()
        else:
            return ExecutionStatus.FAILED, 2, f"Unknown boundary task: {self.task.task_id}"

    def _run_go_boundary(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        task_ev = self.snapshot.task_evidence_dir
        script = self.resolve_script("run_a8_go_boundary.py")
        cmd = [sys.executable, str(script), str(self.snapshot.gonka_dir), str(task_ev)]
        # Use supervisor launcher.log so task_ev is not pre-created before run_a8_go_boundary.py
        log_file = self.snapshot.launcher_log

        self.active_runner = SubprocessRunner(
            argv=cmd,
            cwd=self.snapshot.run_dir,
            log_file=log_file,
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "GO_DOCKER_BUILD",
        )
        code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, code, "Go boundary build/test timed out"
        if code == 0:
            return ExecutionStatus.PASSED, code, None
        return ExecutionStatus.FAILED, code, f"Go boundary failed with exit code {code}"

    def _run_wasm_abi_boundary(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        task_ev = self.snapshot.task_evidence_dir
        task_ev.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.task.stage_timeout_seconds
        self.current_phase = "CARGO_BUILD_WASM"
        # The selected checkout may need crates absent from the runner cache.
        # Cargo.lock pins versions while allowing missing dependencies to be fetched.
        cargo_cmd = [
            "cargo", "build", "--locked", "--release",
            "--target", "wasm32-unknown-unknown",
            "-p", "a8-query-boundary",
        ]
        build_log = task_ev / "wasm-build.log"
        self.active_runner = SubprocessRunner(
            argv=cargo_cmd,
            cwd=self.snapshot.marketplace_dir,
            log_file=build_log,
            env=self.cargo_environment(),
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "CARGO_BUILD",
        )
        b_code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, b_code, "Wasm build timed out"
        if b_code != 0:
            return ExecutionStatus.FAILED, b_code, f"Cargo build a8-query-boundary failed with exit {b_code}"

        wasm_path = (
            self.cargo_target_dir / "wasm32-unknown-unknown" / "release" / "a8_query_boundary.wasm"
        )
        if not wasm_path.is_file():
            return ExecutionStatus.FAILED, 1, f"Built wasm not found: {wasm_path}"

        evidence_wasm = task_ev / "a8_query_boundary.wasm"
        copy_checked_runtime_file(
            self.snapshot.run_dir, wasm_path,
            self.snapshot.run_dir, evidence_wasm,
        )

        self.current_phase = "NODE_ABI_PROBE"
        probe_script = self.resolve_script("test_wasm_query_boundary.mjs")
        abi_json = task_ev / "abi.json"
        # Probe the exact copy retained as evidence, rather than reopening the
        # Cargo output that could change after the checked copy finishes.
        node_cmd = ["node", str(probe_script), str(evidence_wasm), str(abi_json)]
        node_log = task_ev / "node-probe.log"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ExecutionStatus.TIMED_OUT, -1, "Wasm task budget exhausted before ABI probe"

        self.active_runner = SubprocessRunner(
            argv=node_cmd,
            cwd=self.snapshot.marketplace_dir,
            log_file=node_log,
            timeout_seconds=remaining,
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "NODE_ABI_TEST",
        )
        n_code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, n_code, "Wasm ABI probe timed out"
        if n_code == 0 and abi_json.exists():
            return ExecutionStatus.PASSED, n_code, None
        return ExecutionStatus.FAILED, n_code, f"Node ABI probe failed with exit {n_code}"

    def _run_ct_network_unconfirmed(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        task_ev = self.snapshot.task_evidence_dir
        task_ev.mkdir(parents=True, exist_ok=True)
        # A plan packages source commits, not every crate archive.  The runner's
        # registry cache may legitimately be cold on its first use, so forcing
        # Cargo offline turns a valid locked test into a cache-state failure.
        # ``--locked`` still pins the complete dependency graph from Cargo.lock.
        cmd = [
            "cargo", "test", "--locked",
            "-p", "marketplace-deal",
            "network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
            "--", "--nocapture",
        ]
        log_file = task_ev / "ct-network-unconfirmed.log"
        self.active_runner = SubprocessRunner(
            argv=cmd,
            cwd=self.snapshot.marketplace_dir,
            log_file=log_file,
            env=self.cargo_environment(),
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "CARGO_TEST_UNCONFIRMED",
        )
        code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, code, "CT test timed out"
        if code == 0:
            return ExecutionStatus.PASSED, code, None
        return ExecutionStatus.FAILED, code, f"CT test failed with exit code {code}"

    def _run_ct_claim_expiry(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        task_ev = self.snapshot.task_evidence_dir
        task_ev.mkdir(parents=True, exist_ok=True)
        # See _run_ct_network_unconfirmed: these contract tests must be
        # reproducible from Cargo.lock, but must not require a prewarmed cache.
        cmd = [
            "cargo", "test", "--locked",
            "-p", "marketplace-deal",
            "claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
            "--", "--nocapture",
        ]
        log_file = task_ev / "ct-claim-expiry.log"
        self.active_runner = SubprocessRunner(
            argv=cmd,
            cwd=self.snapshot.marketplace_dir,
            log_file=log_file,
            env=self.cargo_environment(),
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "CARGO_TEST_CLAIM_EXPIRY",
        )
        code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, code, "CT test timed out"
        if code == 0:
            return ExecutionStatus.PASSED, code, None
        return ExecutionStatus.FAILED, code, f"CT test failed with exit code {code}"

    def _run_ct_package_c_policy(self) -> Tuple[ExecutionStatus, Optional[int], Optional[str]]:
        """Run every named replacement case and retain its exact case markers."""
        task_ev = self.snapshot.task_evidence_dir
        task_ev.mkdir(parents=True, exist_ok=True)
        cmd = [
            "cargo", "test", "--locked",
            "-p", "marketplace-deal",
            "-p", "marketplace-factory",
            "c_", "--", "--nocapture", "--test-threads=1",
        ]
        log_file = task_ev / "ct-package-c-policy.log"
        self.active_runner = SubprocessRunner(
            argv=cmd,
            cwd=self.snapshot.marketplace_dir,
            log_file=log_file,
            env=self.cargo_environment(),
            timeout_seconds=float(self.task.stage_timeout_seconds),
            heartbeat_callback=self.heartbeat_callback,
            phase_getter=lambda: "CARGO_TEST_PACKAGE_C_POLICY",
        )
        code = self.active_runner.run()
        if self.active_runner.timed_out:
            return ExecutionStatus.TIMED_OUT, code, "Package C policy tests timed out"
        if code == 0:
            return ExecutionStatus.PASSED, code, None
        return ExecutionStatus.FAILED, code, f"Package C policy tests failed with exit code {code}"
