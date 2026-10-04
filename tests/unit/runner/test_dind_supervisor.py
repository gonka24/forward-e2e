"""Unit tests for DinDSupervisor lifecycle, lock acquisition, readiness probe, and termination.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from forward_e2e.suite.supervisor import DinDSupervisor
from forward_e2e.suite.lock import LockContentionError, RuntimeLock
from forward_e2e.suite.runtime import SuiteRuntimeError


class DinDSupervisorTests(unittest.TestCase):
    def setUp(self):
        # start_dockerd normalizes Docker variables in the process environment.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-dind-")
        self.addCleanup(self.tmp_dir.cleanup)
        self.root = Path(self.tmp_dir.name).resolve()
        self.workspace = self.root / "workspace"
        self.data_root = self.root / "docker-data"
        self.socket_path = self.root / "docker.sock"
        self.pid_file = self.root / "docker.pid"
        self.log_file = self.workspace / "dockerd.log"

        self.workspace.mkdir(parents=True)
        self.data_root.mkdir(parents=True)

    def _create_supervisor(self) -> DinDSupervisor:
        supervisor = DinDSupervisor(
            workspace_dir=self.workspace,
            data_root=self.data_root,
            socket_path=self.socket_path,
            pid_file=self.pid_file,
            log_file=self.log_file,
        )
        # Release real descriptors even when daemon cleanup is mocked or an
        # assertion fails. Registered last, this runs before directory removal.
        self.addCleanup(supervisor.lock.release)
        return supervisor

    def test_lock_acquired_before_dockerd_start(self):
        supervisor = self._create_supervisor()

        # Place an active lock on the same lockfile to simulate another container
        other_lock = RuntimeLock(self.workspace / "exclusive.lock")
        other_lock.acquire()
        try:
            with patch("subprocess.Popen") as mock_popen:
                with self.assertRaises(LockContentionError):
                    supervisor.start_dockerd(timeout_seconds=5.0)
                # dockerd must NOT have been launched if lock acquisition failed
                mock_popen.assert_not_called()
        finally:
            other_lock.release()

    def test_is_daemon_ready_probes_private_socket(self):
        supervisor = self._create_supervisor()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            self.assertTrue(supervisor.is_daemon_ready())
            mock_run.assert_called_once()
            cmd, kwargs = mock_run.call_args
            self.assertEqual(cmd[0], ["docker", "info"])
            self.assertEqual(kwargs["env"]["DOCKER_HOST"], f"unix://{supervisor.socket_path}")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            self.assertFalse(supervisor.is_daemon_ready())

    def test_start_dockerd_fails_if_unmanaged_daemon_responsive(self):
        supervisor = self._create_supervisor()

        with patch.object(supervisor, "is_daemon_ready", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                with self.assertRaises(SuiteRuntimeError) as cm:
                    supervisor.start_dockerd(timeout_seconds=5.0, allow_existing=False)
                self.assertIn("Refusing to use an unverified, pre-existing daemon", str(cm.exception))
                self.assertFalse(supervisor.lock.is_acquired)
                mock_popen.assert_not_called()

    def test_start_dockerd_short_circuits_if_already_responsive(self):
        supervisor = self._create_supervisor()

        with patch.object(supervisor, "is_daemon_ready", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                supervisor.start_dockerd(timeout_seconds=5.0, allow_existing=True)
                self.assertTrue(supervisor.lock.is_acquired)
                mock_popen.assert_not_called()

        with patch("os.killpg") as mock_killpg:
            self.assertTrue(supervisor.stop_dockerd())
            mock_killpg.assert_not_called()
        self.assertFalse(supervisor.lock.is_acquired)

    def test_prelaunch_setup_failure_releases_lock_when_no_daemon_was_started(self):
        supervisor = self._create_supervisor()
        self.data_root.rmdir()
        self.data_root.write_text("not a directory", encoding="utf-8")

        with patch.object(supervisor, "is_daemon_ready", return_value=False), \
             patch("subprocess.Popen") as launch:
            with self.assertRaises(OSError):
                supervisor.start_dockerd(timeout_seconds=5.0)
            launch.assert_not_called()

        self.assertFalse(supervisor.lock.is_acquired)
        self.assertFalse(supervisor._owns_dockerd)
        with RuntimeLock(self.workspace / "exclusive.lock") as contender:
            self.assertTrue(contender.is_acquired)

    def test_start_dockerd_cleans_stale_socket_and_pid(self):
        supervisor = self._create_supervisor()

        # Create stale files
        self.socket_path.write_text("stale socket")
        self.pid_file.write_text("12345")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 9999

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(supervisor, "is_daemon_ready", side_effect=[False, True]), \
             patch("forward_e2e.suite.supervisor.time.monotonic", return_value=0.0), \
             patch("time.sleep"):
            supervisor.start_dockerd(timeout_seconds=5.0)
            self.assertFalse(self.socket_path.exists())
            self.assertFalse(self.pid_file.exists())
            self.assertTrue(supervisor._owns_dockerd)
            self.assertIs(supervisor.proc, mock_proc)

    def test_start_dockerd_fails_if_process_exits_early(self):
        supervisor = self._create_supervisor()
        self.log_file.write_text("Error: storage driver overlay2 not supported\nFatal dockerd error")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        mock_proc.pid = 8888

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(supervisor, "is_daemon_ready", return_value=False), \
             patch("forward_e2e.suite.supervisor.time.monotonic", side_effect=[0.0, 0.0, 5.0]), \
             patch.object(supervisor, "stop_dockerd") as mock_stop:
            with self.assertRaises(SuiteRuntimeError) as cm:
                supervisor.start_dockerd(timeout_seconds=5.0)
            self.assertIn("exited unexpectedly with code 1", str(cm.exception))
            self.assertIn("Fatal dockerd error", str(cm.exception))
            mock_stop.assert_called_once()

    def test_start_dockerd_fails_on_readiness_timeout(self):
        supervisor = self._create_supervisor()
        self.log_file.write_text("Listening for connections... still waiting...")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 8888

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch.object(supervisor, "is_daemon_ready", return_value=False), \
             patch.object(supervisor, "stop_dockerd") as mock_stop, \
             patch("forward_e2e.suite.supervisor.time.monotonic", side_effect=[0.0, 0.0, 5.0]), \
             patch("time.sleep") as mock_sleep:
            # Allow one failed readiness probe, then reach the deadline.
            with self.assertRaises(SuiteRuntimeError) as cm:
                supervisor.start_dockerd(timeout_seconds=5.0)
            self.assertIn("failed to become responsive within 5.0s", str(cm.exception))
            mock_sleep.assert_called_once_with(0.5)
            mock_stop.assert_called_once()

    def test_stop_dockerd_graceful_sigterm_and_lock_release(self):
        supervisor = self._create_supervisor()
        supervisor.lock.acquire()
        supervisor._owns_dockerd = True

        mock_proc = MagicMock()
        mock_proc.pid = 7777
        # First poll during entry check: None, then 0 when process exits
        mock_proc.poll.side_effect = [None, 0, 0]
        supervisor.proc = mock_proc

        with patch("os.killpg") as mock_killpg, patch("time.sleep"), \
             patch("forward_e2e.suite.supervisor.time.monotonic", return_value=0.0):
            self.assertTrue(supervisor.stop_dockerd(timeout_seconds=5.0))
            mock_killpg.assert_called_once_with(7777, signal.SIGTERM)
            self.assertFalse(supervisor.lock.is_acquired)
            self.assertIsNone(supervisor.proc)
            self.assertFalse(supervisor._owns_dockerd)

    def _create_unresponsive_daemon(self):
        supervisor = self._create_supervisor()
        supervisor.lock.acquire()
        supervisor._owns_dockerd = True

        mock_proc = MagicMock()
        mock_proc.pid = 6666
        mock_proc.returncode = None  # Remains running through the SIGTERM grace period.
        mock_proc.poll.side_effect = lambda: mock_proc.returncode

        def finish_wait(timeout):
            # Popen records signal termination as a negative return code, and
            # subsequent polls must agree with the completed wait.
            mock_proc.returncode = -signal.SIGKILL
            return mock_proc.returncode

        mock_proc.wait.side_effect = finish_wait
        supervisor.proc = mock_proc
        return supervisor, mock_proc

    def test_stop_dockerd_escalates_to_sigkill_and_releases_lock_after_successful_wait(self):
        supervisor, mock_proc = self._create_unresponsive_daemon()

        with patch("os.killpg") as mock_killpg, patch("time.sleep") as mock_sleep, \
             patch("forward_e2e.suite.supervisor.time.monotonic", side_effect=[0.0, 0.0, 5.0]):
            self.assertTrue(supervisor.stop_dockerd(timeout_seconds=5.0))
            mock_sleep.assert_called_once_with(0.5)
            # Must have sent SIGTERM first, then SIGKILL
            self.assertEqual(mock_killpg.call_args_list, [
                call(6666, signal.SIGTERM),
                call(6666, signal.SIGKILL),
            ])
            mock_proc.wait.assert_called_once_with(timeout=5.0)
            self.assertFalse(supervisor.lock.is_acquired)
            self.assertIsNone(supervisor.proc)
            self.assertFalse(supervisor._owns_dockerd)

    def test_stop_dockerd_retains_lock_process_and_ownership_when_sigkill_wait_times_out(self):
        supervisor, mock_proc = self._create_unresponsive_daemon()
        # Only the wait outcome differs from successful SIGKILL cleanup.
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="dockerd", timeout=5.0)

        with patch("os.killpg") as mock_killpg, patch("time.sleep"), \
             patch("forward_e2e.suite.supervisor.time.monotonic", side_effect=[0.0, 0.0, 5.0]):
            self.assertFalse(supervisor.stop_dockerd(timeout_seconds=5.0))
            self.assertEqual(mock_killpg.call_args_list, [
                call(6666, signal.SIGTERM),
                call(6666, signal.SIGKILL),
            ])
            mock_proc.wait.assert_called_once_with(timeout=5.0)
            self.assertTrue(supervisor.lock.is_acquired)
            self.assertIs(supervisor.proc, mock_proc)
            self.assertTrue(supervisor._owns_dockerd)
            competing_lock = RuntimeLock(self.workspace / "exclusive.lock")
            self.addCleanup(competing_lock.release)
            with self.assertRaises(LockContentionError):
                competing_lock.acquire()

    def test_a_failed_sigterm_keeps_the_live_daemon_owned_and_excludes_a_second_runner(self):
        supervisor, process = self._create_unresponsive_daemon()
        with patch("os.killpg", side_effect=PermissionError("signal refused")):
            with self.assertRaises(PermissionError):
                supervisor.stop_dockerd()
        self.assertTrue(supervisor.lock.is_acquired)
        self.assertIs(supervisor.proc, process)
        self.assertTrue(supervisor._owns_dockerd)
        competing_lock = RuntimeLock(self.workspace / "exclusive.lock")
        self.addCleanup(competing_lock.release)
        with self.assertRaises(LockContentionError):
            competing_lock.acquire()

    def test_an_already_exited_owned_daemon_releases_the_storage_lock(self):
        supervisor, process = self._create_unresponsive_daemon()
        process.returncode = 0
        with patch("os.killpg") as send_signal:
            self.assertTrue(supervisor.stop_dockerd())
        send_signal.assert_not_called()
        self.assertFalse(supervisor.lock.is_acquired)
        self.assertIsNone(supervisor.proc)


if __name__ == "__main__":
    unittest.main()
