"""Regression tests for forward_e2e.execution.cancel: Ctrl-C must really stop a build.

Recording "a signal arrived" is not cancellation. A long E2E run spends its
wall time inside ``make``/``docker build`` children, so a token that is only
polled between stages lets the multi-hour build finish before the run gives
up. These tests pin the behaviour that makes the signal effective: the build
child leads its own process group, that group is signalled and escalated only
while it is still ours, a build started after the signal is never spawned, and
the exit code follows the POSIX 128+signal rule.

They also pin the division of labour with the builder. The runner returns the
killed child's result instead of raising, so ``Builder.run_recipe`` can write
the step log and record the command *before* unwinding with ``RUN_CANCELLED``:
a cancelled run must lose no evidence.

Every child process started here is a self-contained, short-lived Python
one-liner belonging to the test itself; no project build is ever invoked.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from forward_e2e.execution.builder import Builder, _default_runner
from forward_e2e.execution.cancel import (
    DEFAULT_GRACE_SECONDS,
    TIMEOUT_GRACE_SECONDS,
    Cancellation,
    _group_alive,
    _stop,
    make_build_runner,
)
from forward_e2e.execution.compat import (
    gonka_immutable_source_adapter,
)
from forward_e2e.execution.errors import ExecutionCancelled
from forward_e2e.execution.runlock import ManifestStatus
from tests.unit.runner.support.fakes import (
    BUILD_STARTED_EPOCH,
    FakeBuildRunner,
    GONKA_VERSION,
    REQUESTED_SHA,
    new_manifest,
    write_gonka_tree,
)

#: Maximum duration of explicit readiness, exit and thread waits in these tests.
WAIT_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.02

#: Short grace period so the SIGTERM -> SIGKILL escalation stays inside a test.
TEST_GRACE_SECONDS = 0.3

HAVE_PROCESS_GROUPS = hasattr(os, "getpgid") and hasattr(os, "killpg")
POSIX_ONLY = "POSIX process groups are required"


class _RegistrationWatchingCancellation(Cancellation):
    """A token that announces when a build's process group has been tracked.

    Used to make "the signal arrives *while* the build runs" deterministic
    instead of relying on a sleep of the right length.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.registered = threading.Event()

    def register_process_group(self, pgid: int) -> None:
        super().register_process_group(pgid)
        self.registered.set()


class SignallingInspectRunner(FakeBuildRunner):
    """Fires the signal exactly when a step's artefacts are being recorded.

    The build command itself has already succeeded by then, which is the window
    the builder used to leave unchecked.
    """

    def __init__(self, token, *, signum=signal.SIGINT, **kwargs):
        super().__init__(**kwargs)
        self.token = token
        self.signum = signum

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        if [str(a) for a in argv][:3] == ["docker", "image", "inspect"]:
            self.token.request(self.signum)
        return super().__call__(argv, cwd=cwd, env=env, timeout=timeout)


class CancellationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-cancel-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.children = []

    def tearDown(self):
        for child in self.children:
            try:
                child.kill()
            except OSError:
                pass
            try:
                child.wait(timeout=WAIT_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if child.stdout is not None:
                child.stdout.close()
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def spawn(self, source: str, **kwargs) -> subprocess.Popen:
        """A harmless child of this test, in its own session like a build."""
        child = subprocess.Popen(
            [sys.executable, "-c", source],
            start_new_session=True,
            **kwargs,
        )
        self.children.append(child)
        return child

    def spawn_sleeper(self, seconds: float = 30.0) -> subprocess.Popen:
        return self.spawn(
            f"import time;time.sleep({seconds})",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def spawn_sigterm_proof_child(self) -> subprocess.Popen:
        """A child that stays alive through SIGTERM, and says when it is ready."""
        marker = self.root / f"sigterm-ready-{len(self.children)}"
        child = self.spawn(
            "import signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"Path({str(marker)!r}).write_text('ready')\n"
            "time.sleep(30)\n",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertTrue(
            self.wait_for_file(marker),
            f"child {child.pid} did not install its SIGTERM handler within "
            f"{WAIT_TIMEOUT_SECONDS} seconds (exit code: {child.poll()})",
        )
        return child

    def wait_for_exit(self, process, *, timeout=WAIT_TIMEOUT_SECONDS):
        """Bounded poll: returns the exit status, or ``None`` if still alive."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = process.poll()
            if status is not None:
                return status
            time.sleep(POLL_SECONDS)
        return process.poll()

    def wait_for_file(self, path, *, timeout=WAIT_TIMEOUT_SECONDS):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(POLL_SECONDS)
        return path.exists()

    def actions(self, token):
        return [record["action"] for record in token.to_dict()["signalled_process_groups"]]


class CancellationTokenStateTests(CancellationTestCase):
    """The token is a one-way switch carrying the operator's exit code."""

    def test_a_fresh_token_is_not_requested_and_reports_a_success_exit_code(self):
        token = Cancellation()

        self.assertFalse(token.requested)
        self.assertIsNone(token.signum)
        self.assertEqual(token.exit_code, 0)

    def test_an_interrupt_switches_the_token_and_reports_exit_code_130(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)

        token.request(signal.SIGINT)

        self.assertTrue(token.requested)
        self.assertEqual(token.signum, int(signal.SIGINT))
        self.assertEqual(token.exit_code, 130)

    def test_a_termination_signal_switches_the_token_and_reports_exit_code_143(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)

        token.request(signal.SIGTERM)

        self.assertTrue(token.requested)
        self.assertEqual(token.signum, int(signal.SIGTERM))
        self.assertEqual(token.exit_code, 143)

    def test_an_unmapped_signal_still_follows_the_posix_128_plus_signal_rule(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        other = getattr(signal, "SIGHUP", signal.SIGTERM)

        token.request(other)

        self.assertEqual(token.exit_code, 128 + int(other))

    def test_the_first_signal_decides_the_exit_code_and_a_second_cannot_change_it(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)

        token.request(signal.SIGINT)
        token.request(signal.SIGTERM)

        self.assertEqual(token.signum, int(signal.SIGINT))
        self.assertEqual(token.exit_code, 130)

    def test_the_first_signal_promises_the_evidence_and_a_second_announces_escalation(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        messages = []

        token.request(signal.SIGINT, emit=messages.append)
        token.request(signal.SIGINT, emit=messages.append)

        self.assertIn("Received a termination signal", messages[0])
        self.assertIn("preserving the evidence", messages[0])
        self.assertIn("Second termination signal", messages[1])

    def test_the_default_grace_period_is_a_bounded_positive_number_of_seconds(self):
        # A build that traps SIGTERM must not be able to hold the runner
        # hostage, so the escalation delay is finite and short.
        self.assertGreater(DEFAULT_GRACE_SECONDS, 0.0)
        self.assertLessEqual(DEFAULT_GRACE_SECONDS, 60.0)

    def test_the_grace_period_a_token_was_given_can_be_read_back(self):
        self.assertEqual(Cancellation().grace_seconds, DEFAULT_GRACE_SECONDS)
        self.assertEqual(Cancellation(grace_seconds=0.25).grace_seconds, 0.25)

    def test_a_build_that_blew_its_own_timeout_gets_the_shorter_grace(self):
        """It has already had every second its recipe asked for."""
        self.assertGreater(TIMEOUT_GRACE_SECONDS, 0.0)
        self.assertLess(TIMEOUT_GRACE_SECONDS, DEFAULT_GRACE_SECONDS)
        self.assertEqual(
            inspect.signature(_stop).parameters["grace_seconds"].default,
            TIMEOUT_GRACE_SECONDS,
        )

    def test_a_fresh_token_reports_an_empty_cancellation_record(self):
        token = Cancellation()

        record = token.to_dict()

        self.assertEqual(record["requested"], False)
        self.assertIsNone(record["signal"])
        self.assertEqual(record["exit_code"], 0)
        self.assertIsNone(record["requested_at_epoch"])
        self.assertEqual(record["signalled_process_groups"], [])

    def test_a_requested_token_reports_the_signal_the_exit_code_and_the_moment(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        before = time.time()

        token.request(signal.SIGTERM)

        record = token.to_dict()
        self.assertEqual(record["requested"], True)
        self.assertEqual(record["signal"], int(signal.SIGTERM))
        self.assertEqual(record["exit_code"], 143)
        self.assertGreaterEqual(record["requested_at_epoch"], before)


class RaiseIfRequestedTests(CancellationTestCase):
    """Stage boundaries are where a cancelled run actually unwinds."""

    def test_asking_before_any_signal_is_a_no_op_that_lets_the_stage_proceed(self):
        token = Cancellation()

        self.assertIsNone(token.raise_if_requested("acquire-sources"))

    def test_asking_after_an_interrupt_raises_with_the_stage_and_exit_code_130(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGINT)

        with self.assertRaises(ExecutionCancelled) as cm:
            token.raise_if_requested("build-chain-images")

        self.assertIn("The run was cancelled by a termination signal", str(cm.exception))
        self.assertEqual(cm.exception.details["stage"], "build-chain-images")
        self.assertEqual(cm.exception.details["signal"], int(signal.SIGINT))
        self.assertEqual(cm.exception.exit_code, 130)
        self.assertEqual(cm.exception.code, "EXECUTION_CANCELLED")

    def test_asking_after_a_termination_signal_raises_with_exit_code_143(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGTERM)

        with self.assertRaises(ExecutionCancelled) as cm:
            token.raise_if_requested("execute-scenarios")

        self.assertIn("the partial evidence is kept exactly as it was", str(cm.exception))
        self.assertEqual(cm.exception.exit_code, 143)
        self.assertEqual(cm.exception.details["stage"], "execute-scenarios")


class BuildRunnerContractTests(CancellationTestCase):
    """The cancellable runner must be a drop-in for the builder's default."""

    def test_cancellation_finishes_group_escalation_when_the_leader_and_its_pipes_exit_first(self):
        token = Cancellation(grace_seconds=0.02)
        with patch("forward_e2e.execution.cancel.subprocess.Popen") as popen, patch(
            "forward_e2e.execution.cancel.os.getpgid", return_value=12345
        ), patch("forward_e2e.execution.cancel.os.killpg") as killpg:
            process = popen.return_value
            process.returncode = -signal.SIGTERM

            def leader_exits(**kwargs):
                token.request(signal.SIGTERM)
                return "partial output", ""

            process.communicate.side_effect = leader_exits
            result = make_build_runner(token)(["make"], timeout=1)
            # The OS boundary reports a remaining group member after the
            # leader exits. Cleanup must finish before the runner returns.
            self.assertIn((12345, signal.SIGKILL), [c.args for c in killpg.call_args_list])
            self.assertEqual(result.stdout, "partial output")
            calls = killpg.call_count
            token.request(signal.SIGTERM)
            self.assertEqual(killpg.call_count, calls, "finished ownership must be released")

    def test_a_detached_pipe_writer_cannot_make_timeout_output_draining_wait_forever(self):
        initial = subprocess.TimeoutExpired(["make"], 1, output=b"partial")
        drain = subprocess.TimeoutExpired(
            ["make"], 2, output=b"partial and shutdown output", stderr=b"shutdown error"
        )
        with patch("forward_e2e.execution.cancel.subprocess.Popen") as popen, patch(
            "forward_e2e.execution.cancel.os.getpgid", side_effect=ProcessLookupError
        ):
            process = popen.return_value
            process.communicate.side_effect = [initial, drain]
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                make_build_runner()(["make"], timeout=1)
            for call in process.communicate.call_args_list:
                self.assertGreater(call.kwargs["timeout"], 0)
            process.stdout.close.assert_called_once()
            process.stderr.close.assert_called_once()
        self.assertIs(caught.exception, initial)
        self.assertEqual(initial.output, b"partial and shutdown output")
        self.assertEqual(initial.stderr, b"shutdown error")

    def test_an_exited_leader_does_not_busy_spin_while_the_group_grace_period_runs(self):
        process = Mock()

        def reaped_leader(*args, **kwargs):
            # A missing sleep must fail this test rather than freezing its
            # virtual clock forever. The limit exceeds the expected 3 ticks.
            self.assertLessEqual(process.wait.call_count, 10, "cleanup is busy-spinning")
            return 0

        process.wait.side_effect = reaped_leader
        elapsed = [0.0]

        def sleep(seconds):
            elapsed[0] += seconds

        with patch("forward_e2e.execution.cancel.os.killpg") as killpg, patch(
            "forward_e2e.execution.cancel.time.monotonic", side_effect=lambda: elapsed[0]
        ), patch("forward_e2e.execution.cancel.time.sleep", side_effect=sleep), patch(
            "forward_e2e.execution.cancel.time.time", side_effect=AssertionError("wall clock used for duration")
        ):
            _stop(process, 12345, grace_seconds=0.3)
        self.assertAlmostEqual(elapsed[0], 0.3)
        self.assertLessEqual(process.wait.call_count, 4)
        self.assertEqual(killpg.call_args.args, (12345, signal.SIGKILL))

    def test_cancellation_grace_uses_elapsed_time_even_when_wall_clock_jumps(self):
        token = Cancellation(grace_seconds=0.3)
        token.register_process_group(12345)
        elapsed = [0.0]

        def sleep(seconds):
            elapsed[0] += seconds

        with patch("forward_e2e.execution.cancel.os.killpg") as killpg, patch(
            "forward_e2e.execution.cancel.time.monotonic", side_effect=lambda: elapsed[0]
        ), patch("forward_e2e.execution.cancel.time.sleep", side_effect=sleep), patch(
            "forward_e2e.execution.cancel.time.time", side_effect=[100.0, 200.0, 300.0]
        ):
            token._escalate_after_grace(12345)
        self.assertAlmostEqual(elapsed[0], 0.3)
        self.assertEqual(killpg.call_args.args, (12345, signal.SIGKILL))

    def test_a_timeout_carries_output_flushed_during_shutdown_to_the_callers_log(self):
        timeout = subprocess.TimeoutExpired(["make"], 1, output=b"partial")
        with patch("forward_e2e.execution.cancel.subprocess.Popen") as popen, patch(
            "forward_e2e.execution.cancel.os.getpgid", side_effect=ProcessLookupError
        ):
            process = popen.return_value
            process.communicate.side_effect = [
                timeout, ("partial and shutdown output", "shutdown error")
            ]
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                make_build_runner()(["make"], timeout=1)

        self.assertIs(caught.exception, timeout)
        self.assertEqual(caught.exception.stdout, "partial and shutdown output")
        self.assertEqual(caught.exception.stderr, "shutdown error")
        process.terminate.assert_called_once()

    def described(self, function):
        return [
            (p.name, p.kind, p.default)
            for p in inspect.signature(function).parameters.values()
        ]

    def test_the_cancellable_runner_has_the_same_call_signature_as_the_default_runner(self):
        runner = make_build_runner(Cancellation())

        self.assertEqual(self.described(runner), self.described(_default_runner))

    def test_the_runner_returns_a_completed_process_carrying_the_childs_output(self):
        runner = make_build_runner(Cancellation(grace_seconds=TEST_GRACE_SECONDS))
        argv = [sys.executable, "-c", "import sys;print('out');sys.stderr.write('err')"]

        completed = runner(argv, timeout=WAIT_TIMEOUT_SECONDS)

        self.assertIsInstance(completed, subprocess.CompletedProcess)
        self.assertEqual(completed.args, argv)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "out")
        self.assertEqual(completed.stderr.strip(), "err")

    def test_the_runner_reports_a_failing_child_instead_of_raising(self):
        runner = make_build_runner(Cancellation(grace_seconds=TEST_GRACE_SECONDS))

        completed = runner(
            [sys.executable, "-c", "raise SystemExit(3)"], timeout=WAIT_TIMEOUT_SECONDS
        )

        self.assertEqual(completed.returncode, 3)

    def test_the_runner_honours_the_working_directory_and_environment_it_is_given(self):
        runner = make_build_runner(Cancellation(grace_seconds=TEST_GRACE_SECONDS))
        argv = [sys.executable, "-c", "import os;print(os.environ['A8_MARKER']);print(os.getcwd())"]

        completed = runner(
            argv,
            cwd=str(self.root),
            env={"A8_MARKER": "selected-sources", "PATH": os.environ.get("PATH", "")},
            timeout=WAIT_TIMEOUT_SECONDS,
        )

        marker, directory = completed.stdout.splitlines()
        self.assertEqual(marker, "selected-sources")
        self.assertEqual(Path(directory).resolve(), self.root)

    def test_a_runner_without_a_token_still_executes_the_child(self):
        runner = make_build_runner()

        completed = runner(
            [sys.executable, "-c", "print('no token')"], timeout=WAIT_TIMEOUT_SECONDS
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "no token")


@unittest.skipUnless(HAVE_PROCESS_GROUPS, POSIX_ONLY)
class BuildProcessGroupTests(CancellationTestCase):
    """Only a whole process group can stop make, docker and their children."""

    def test_the_build_child_leads_its_own_process_group_so_the_whole_tree_is_reachable(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        runner = make_build_runner(token)

        completed = runner(
            [sys.executable, "-c", "import os;print(os.getpgid(0))"],
            timeout=WAIT_TIMEOUT_SECONDS,
        )

        self.assertEqual(completed.returncode, 0)
        child_pgid = int(completed.stdout.strip())
        self.assertNotEqual(child_pgid, os.getpgid(0))

    def test_a_finished_build_is_unregistered_so_a_later_signal_targets_nothing(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        runner = make_build_runner(token)
        runner([sys.executable, "-c", "print('done')"], timeout=WAIT_TIMEOUT_SECONDS)

        token.request(signal.SIGTERM)

        self.assertEqual(token.to_dict()["signalled_process_groups"], [])

    def test_a_build_group_registered_after_the_signal_is_stopped_immediately(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        child = self.spawn_sleeper()
        pgid = os.getpgid(child.pid)
        token.request(signal.SIGTERM)

        token.register_process_group(pgid)

        self.assertIsNotNone(
            self.wait_for_exit(child), "the registered process group was never signalled"
        )
        record = token.to_dict()
        self.assertTrue(record["signalled_process_groups"])
        first = record["signalled_process_groups"][0]
        self.assertEqual(first["pgid"], pgid)
        self.assertEqual(first["action"], "terminate")

    def test_an_unregistered_group_is_left_alone_when_the_signal_finally_arrives(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        child = self.spawn_sleeper()
        pgid = os.getpgid(child.pid)
        token.register_process_group(pgid)
        token.unregister_process_group(pgid)

        token.request(signal.SIGTERM)

        self.assertEqual(token.to_dict()["signalled_process_groups"], [])
        self.assertIsNone(child.poll())

    def test_a_build_that_ignores_the_polite_signal_is_killed_after_the_grace(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        child = self.spawn_sigterm_proof_child()
        pgid = os.getpgid(child.pid)
        token.register_process_group(pgid)

        token.request(signal.SIGTERM)

        self.assertIsNotNone(
            self.wait_for_exit(child), "the build survived both the SIGTERM and the grace"
        )
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(self.actions(token), ["terminate", "kill"])

    def test_a_live_group_unregistered_during_the_grace_receives_no_later_kill(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        child = self.spawn_sigterm_proof_child()
        pgid = os.getpgid(child.pid)
        token.register_process_group(pgid)

        token.request(signal.SIGTERM)
        # Release ownership while the group is still alive. The delayed
        # escalation must respect that release rather than signal it again.
        token.unregister_process_group(pgid)

        deadline = time.monotonic() + TEST_GRACE_SECONDS * 4
        while time.monotonic() < deadline and "kill" not in self.actions(token):
            time.sleep(POLL_SECONDS)
        self.assertEqual(self.actions(token), ["terminate"])
        self.assertIsNone(child.poll(), "a process group we no longer own was killed")

    def test_only_a_definite_absence_of_the_group_counts_as_gone(self):
        # Probe the three OS outcomes directly.  A container's PID-1/session
        # arrangement is allowed to make its own process group unprobeable,
        # so using os.getpgid(0) here made the unit test environment-specific.
        with patch("forward_e2e.execution.cancel.os.killpg", return_value=None):
            self.assertTrue(_group_alive(123))
        with patch(
            "forward_e2e.execution.cancel.os.killpg", side_effect=PermissionError("not ours")
        ):
            self.assertTrue(_group_alive(123))
        with patch(
            "forward_e2e.execution.cancel.os.killpg", side_effect=ProcessLookupError("gone")
        ):
            self.assertFalse(_group_alive(123))

    def test_a_build_that_outlives_its_timeout_is_asked_politely_before_being_killed(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        elapsed = [0.0]
        signals = []

        def sleep(seconds):
            elapsed[0] += seconds

        def killpg(pgid, sig):
            if sig:
                signals.append((pgid, sig, elapsed[0]))

        # Model a running group that ignores TERM. No wall-clock deadline can
        # race Python startup or installation of a child signal handler.
        with patch("forward_e2e.execution.cancel.subprocess.Popen") as popen, patch(
            "forward_e2e.execution.cancel.os.getpgid", return_value=12345
        ), patch("forward_e2e.execution.cancel.os.killpg", side_effect=killpg), patch(
            "forward_e2e.execution.cancel.time.monotonic", side_effect=lambda: elapsed[0]
        ), patch("forward_e2e.execution.cancel.time.sleep", side_effect=sleep):
            process = popen.return_value
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(["make"], 1), ("partial output", ""),
            ]

            def still_running(*args, **kwargs):
                self.assertLessEqual(process.wait.call_count, 10, "cleanup is busy-spinning")
                raise subprocess.TimeoutExpired(["make"], 0.1)

            process.wait.side_effect = still_running
            with self.assertRaises(subprocess.TimeoutExpired):
                make_build_runner(token, timeout_grace_seconds=0.3)(["make"], timeout=1)

            self.assertEqual([s[:2] for s in signals], [
                (12345, signal.SIGTERM), (12345, signal.SIGKILL),
            ])
            self.assertEqual(signals[0][2], 0.0)
            self.assertAlmostEqual(signals[1][2], 0.3)
            token.request(signal.SIGTERM)
            self.assertEqual(len(signals), 2, "completed ownership was not released")


@unittest.skipUnless(HAVE_PROCESS_GROUPS, POSIX_ONLY)
class RunnerCancellationTests(CancellationTestCase):
    """What the runner itself does when the operator gives up."""

    def test_a_build_started_after_the_signal_is_refused_before_anything_is_spawned(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGINT)
        runner = make_build_runner(token)
        marker = self.root / "the-child-ran"
        argv = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]

        with self.assertRaises(ExecutionCancelled) as cm:
            runner(argv, timeout=WAIT_TIMEOUT_SECONDS)

        self.assertIn("Nothing after this point was executed", str(cm.exception))
        self.assertEqual(cm.exception.details["stage"], "build-step-start")
        self.assertEqual(cm.exception.exit_code, 130)
        self.assertFalse(marker.exists(), "the build child was spawned after cancellation")

    def test_a_signal_during_the_build_kills_the_child_and_still_returns_its_result(self):
        token = _RegistrationWatchingCancellation(grace_seconds=TEST_GRACE_SECONDS)
        runner = make_build_runner(token)

        def cancel_once_the_build_is_tracked():
            if token.registered.wait(timeout=WAIT_TIMEOUT_SECONDS):
                token.request(signal.SIGTERM)

        canceller = threading.Thread(target=cancel_once_the_build_is_tracked, daemon=True)
        canceller.start()
        try:
            completed = runner(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                timeout=WAIT_TIMEOUT_SECONDS,
            )
        finally:
            canceller.join(timeout=WAIT_TIMEOUT_SECONDS)

        # The runner does not raise: the caller still has to write the log and
        # record the command before the run unwinds.
        self.assertIsInstance(completed, subprocess.CompletedProcess)
        self.assertEqual(completed.returncode, -signal.SIGTERM)
        self.assertEqual(self.actions(token)[0], "terminate")
        self.assertEqual(token.to_dict()["exit_code"], 143)


class CancelledBuildStepTests(CancellationTestCase):
    """A cancelled build step keeps every piece of evidence it produced."""

    def setUp(self):
        super().setUp()
        self.gonka = write_gonka_tree(self.root / "gonka")
        self.build_out = self.root / "build-out"
        self.build_out.mkdir(parents=True)
        self.log_dir = self.root / "logs"
        self.context = {
            "gonka": str(self.gonka),
            "build_out": str(self.build_out),
            "platform": "linux/amd64",
            "goarch": "amd64",
            "gonka_sha": REQUESTED_SHA,
            "gonka_version": GONKA_VERSION,
        }
        self.roles = {"gonka": self.gonka, "build_out": self.build_out}
        base_recipe = gonka_immutable_source_adapter().build
        base_step = next(s for s in base_recipe.steps if s.step_id == "inferenced-image")
        self.recipe = dataclasses.replace(
            base_recipe,
            steps=(
                dataclasses.replace(
                    base_step,
                    produces_images=tuple(base_recipe.chain_images[:5]),
                ),
            ),
        )

    def run_chain_build(self, *, token, runner=None, recipe=None, manifest=None):
        manifest = manifest if manifest is not None else new_manifest()
        builder = Builder(
            runner=runner if runner is not None else FakeBuildRunner(),
            log_dir=self.log_dir,
            cancellation=token,
        )
        builder.run_recipe(
            recipe if recipe is not None else self.recipe,
            manifest=manifest,
            context=self.context,
            roles=self.roles,
            started_epoch=BUILD_STARTED_EPOCH,
        )
        return manifest

    def test_a_requested_token_stops_the_recipe_at_the_step_that_was_running(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGTERM)
        manifest = new_manifest()

        with self.assertRaises(ExecutionCancelled) as cm:
            self.run_chain_build(token=token, manifest=manifest)

        self.assertIn("The run was cancelled by a termination signal", str(cm.exception))
        self.assertEqual(cm.exception.details["stage"], "build-step:inferenced-image")
        self.assertEqual(cm.exception.exit_code, 143)

    def test_the_cancelled_step_is_recorded_as_cancelled_not_as_a_verdict(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGINT)
        manifest = new_manifest()

        with self.assertRaises(ExecutionCancelled):
            self.run_chain_build(token=token, manifest=manifest)

        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "RUN_CANCELLED")
        self.assertIn("The operator stopped the run", failure["message"])
        self.assertEqual(failure["details"]["step_id"], "inferenced-image")
        self.assertEqual(manifest.status, ManifestStatus.FAILED)

    def test_the_killed_steps_log_and_command_record_survive_the_cancellation(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        manifest = new_manifest()

        def killed_runner(argv, **kwargs):
            token.request(signal.SIGTERM)
            return subprocess.CompletedProcess(
                argv, -signal.SIGTERM, "compiler stdout\n", "compiler stderr\n"
            )

        with self.assertRaises(ExecutionCancelled):
            self.run_chain_build(token=token, runner=killed_runner, manifest=manifest)

        log = self.log_dir / "build-inferenced-image.log"
        self.assertTrue(log.is_file(), "the cancelled step left no log behind")
        self.assertEqual(log.read_text(encoding="utf-8"), "compiler stdout\ncompiler stderr\n")
        self.assertEqual(len(manifest.build_commands), 1)
        record = manifest.build_commands[0]
        self.assertEqual(record["step_id"], "inferenced-image")
        self.assertEqual(record["argv"][:2], ["docker", "build"])
        self.assertTrue(record["completed_at_utc"])
        self.assertEqual(record["exit_code"], -signal.SIGTERM)
        self.assertEqual([f["code"] for f in manifest.failures], ["RUN_CANCELLED"])

    def test_no_image_is_claimed_as_built_by_a_cancelled_step(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGTERM)
        manifest = new_manifest()

        with self.assertRaises(ExecutionCancelled):
            self.run_chain_build(token=token, manifest=manifest)

        self.assertEqual(manifest.images, [])
        self.assertEqual(manifest.binaries, [])
        self.assertEqual(manifest.wasm, [])

    def test_a_later_step_is_never_started_once_the_run_is_cancelled(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        token.request(signal.SIGTERM)
        runner = FakeBuildRunner()
        manifest = new_manifest()
        first_step = self.recipe.steps[0]
        two_step_recipe = dataclasses.replace(
            self.recipe,
            steps=(
                first_step,
                dataclasses.replace(first_step, step_id="must-not-start"),
            ),
        )

        with self.assertRaises(ExecutionCancelled):
            self.run_chain_build(
                token=token, runner=runner, recipe=two_step_recipe, manifest=manifest
            )

        self.assertEqual([call["argv"][:2] for call in runner.calls], [["docker", "build"]])
        self.assertEqual(len(manifest.build_commands), 1)

    def test_a_step_killed_by_the_signal_is_reported_as_cancelled_not_as_failed(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        # The build command died because the operator stopped it, so its
        # non-zero exit code is not a verdict about the selected sources.
        def runner(argv, **kwargs):
            token.request(signal.SIGTERM)
            return subprocess.CompletedProcess(
                argv, -signal.SIGTERM, "partial build output\n", "terminated\n"
            )

        manifest = new_manifest()

        with self.assertRaises(ExecutionCancelled):
            self.run_chain_build(token=token, runner=runner, manifest=manifest)

        self.assertEqual([f["code"] for f in manifest.failures], ["RUN_CANCELLED"])
        self.assertNotIn(
            "BUILD_STEP_FAILED", [f["code"] for f in manifest.failures]
        )

    def test_a_build_with_a_token_that_was_never_signalled_runs_to_completion(self):
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        recipe = dataclasses.replace(
            self.recipe,
            steps=(
                dataclasses.replace(
                    self.recipe.steps[0],
                    produces_images=("ghcr.io/product-science/inferenced:latest",),
                ),
            ),
        )

        manifest = self.run_chain_build(token=token, recipe=recipe)

        self.assertEqual(manifest.failures, [])
        self.assertEqual(len(manifest.images), 1)
        self.assertFalse(token.requested)

    def test_a_build_without_any_token_is_unaffected(self):
        manifest = self.run_chain_build(token=None)

        self.assertEqual(manifest.failures, [])
        self.assertEqual(len(manifest.images), 5)

    def test_a_signal_that_lands_while_the_artefacts_are_recorded_stops_that_step(self):
        """Recording one step's images is several `docker image inspect` calls.

        A signal arriving in that window used to go unnoticed until the next
        step began -- and on the last step there is no next step, so the recipe
        returned normally and the run carried on into the suite.
        """
        token = Cancellation(grace_seconds=TEST_GRACE_SECONDS)
        runner = SignallingInspectRunner(token, signum=signal.SIGINT)
        manifest = new_manifest()

        with self.assertRaises(ExecutionCancelled) as cm:
            self.run_chain_build(token=token, runner=runner, manifest=manifest)

        self.assertEqual(cm.exception.details["stage"], "build-step:inferenced-image")
        self.assertEqual(cm.exception.exit_code, 130)
        self.assertEqual(manifest.failures[-1]["code"], "RUN_CANCELLED")
        # The step ran to the end, so everything it produced is still recorded.
        self.assertEqual(manifest.build_commands[-1]["exit_code"], 0)
        self.assertTrue(manifest.images, "the recorded images were discarded")
        self.assertTrue((self.log_dir / "build-inferenced-image.log").is_file())


if __name__ == "__main__":
    unittest.main()
