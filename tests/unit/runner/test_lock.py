"""Unit tests for A8 runtime locking and deadlock prevention.

Ensures:
- Exclusive lock competition fails fast on contention
- Same instance nesting retains exclusion until the outer context exits
- Lock release never deletes the lock file

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import errno
import os
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from forward_e2e.suite.lock import LockContentionError, LockOperationError, RuntimeLock


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-lock-")
        self.lock_file = Path(self.tmp_dir.name) / "exclusive.lock"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_leaving_the_lock_context_releases_it_without_deleting_the_file(self):
        lock = RuntimeLock(self.lock_file)
        self.assertFalse(lock.is_acquired)

        with lock:
            self.assertTrue(lock.is_acquired)
            self.assertTrue(self.lock_file.is_file())

        self.assertFalse(lock.is_acquired)
        self.assertTrue(self.lock_file.is_file())

    def test_an_exception_in_the_lock_context_propagates_and_releases_the_lock_for_a_competitor(self):
        lock = RuntimeLock(self.lock_file)
        self.addCleanup(lock.release)
        error = ValueError("synthetic failure inside the lock context")

        with self.assertRaises(ValueError) as raised:
            with lock:
                self.assertTrue(lock.is_acquired)
                raise error

        self.assertIs(raised.exception, error)
        self.assertFalse(lock.is_acquired)
        self.assertTrue(self.lock_file.is_file())
        with RuntimeLock(self.lock_file) as contender:
            self.assertTrue(contender.is_acquired)

    def test_nested_contexts_on_the_same_instance_exclude_competitors_until_outer_exit(self):
        """Nested runtime actions must not release the orchestrator's outer lock."""
        lock = RuntimeLock(self.lock_file)
        contender = RuntimeLock(self.lock_file)
        self.addCleanup(contender.release)
        with lock:
            self.assertTrue(lock.is_acquired)

            with lock:
                self.assertTrue(lock.is_acquired)
                with self.assertRaises(LockContentionError):
                    contender.acquire()

            # Internal counters alone cannot prove that the OS lock is still held.
            self.assertTrue(lock.is_acquired)
            with self.assertRaises(LockContentionError):
                contender.acquire()

        self.assertFalse(lock.is_acquired)
        with contender:
            self.assertTrue(contender.is_acquired)

    def test_a_failed_contender_can_retry_after_release_without_deleting_the_file(self):
        lock1 = RuntimeLock(self.lock_file)
        lock2 = RuntimeLock(self.lock_file)
        self.addCleanup(lock2.release)
        with lock1:
            self.assertTrue(lock1.is_acquired)

            with self.assertRaises(LockContentionError):
                lock2.acquire()
            self.assertFalse(lock2.is_acquired)

            self.assertTrue(self.lock_file.is_file())

        with lock2:
            self.assertTrue(lock2.is_acquired)

    def test_acquisition_requests_exclusive_nonblocking_flock_and_exit_unlocks_the_same_fd(self):
        with patch("forward_e2e.suite.lock.fcntl.flock") as flock:
            with RuntimeLock(self.lock_file):
                flock.assert_called_once()
                fd, flags = flock.call_args.args
                self.assertEqual(flags, fcntl.LOCK_EX | fcntl.LOCK_NB)

            self.assertEqual(flock.call_args_list, [
                call(fd, fcntl.LOCK_EX | fcntl.LOCK_NB),
                call(fd, fcntl.LOCK_UN),
            ])

    def test_non_contention_lock_errors_preserve_the_os_failure_and_close_the_descriptor(self):
        for number in (errno.ENOLCK, errno.EIO, errno.EBADF):
            with self.subTest(errno=number):
                lock = RuntimeLock(self.lock_file)
                failure = OSError(number, "synthetic flock failure")
                with patch("forward_e2e.suite.lock.fcntl.flock", side_effect=failure) as flock, patch(
                    "forward_e2e.suite.lock.os.close", wraps=os.close
                ) as close:
                    with self.assertRaises(LockOperationError) as raised:
                        lock.acquire()
                self.assertIs(raised.exception.__cause__, failure)
                self.assertEqual(raised.exception.code, "RUNTIME_LOCK_FAILED")
                self.assertFalse(lock.is_acquired)
                close.assert_called_once_with(flock.call_args.args[0])

    def test_only_contention_errnos_are_reported_as_another_active_runner(self):
        for number in (errno.EAGAIN, errno.EACCES):
            with self.subTest(errno=number):
                with patch("forward_e2e.suite.lock.fcntl.flock", side_effect=OSError(number, "busy")):
                    with self.assertRaises(LockContentionError):
                        RuntimeLock(self.lock_file).acquire()

    def test_cli_reports_lock_service_failure_as_infrastructure_failure_without_running_tasks(self):
        from forward_e2e.execution.cli import _with_inner_dockerd
        from unittest.mock import Mock

        emit = Mock()
        action = Mock()
        with patch("forward_e2e.suite.supervisor.DinDSupervisor") as supervisor, patch("signal.signal"):
            supervisor.return_value.start_dockerd.side_effect = LockOperationError("synthetic ENOLCK")
            self.assertEqual(_with_inner_dockerd(action, workspace_dir=self.lock_file.parent, emit=emit), 1)
        action.assert_not_called()
        message = emit.call_args.args[0]
        self.assertIn("Infrastructure error", message)
        self.assertIn("synthetic ENOLCK", message)
        self.assertNotIn("another runner", message)


if __name__ == "__main__":
    unittest.main()
