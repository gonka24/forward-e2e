"""Regression tests for task adapter process boundaries.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import io
from itertools import count
import os
import signal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ops.a8.adapters import BoundaryTaskAdapter, NativeTaskAdapter, ProcessExecutionError, SubprocessRunner
from ops.a8.catalog import get_task_by_id_or_alias
from ops.a8.collector import CollectorSecurityError
from ops.a8.runtime import RuntimeSnapshot


class NativeTaskAdapterWorkingDirectoryTests(unittest.TestCase):
    def test_native_harness_starts_in_the_isolated_marketplace_checkout(self):
        """The runtime evidence directory is not a Rust workspace and cannot be Cargo's cwd."""
        with tempfile.TemporaryDirectory(prefix="a8-adapter-") as temporary:
            root = Path(temporary)
            snapshot = RuntimeSnapshot(root, "adapter-cwd-001")
            snapshot.run_dir.mkdir(parents=True)
            snapshot.marketplace_dir.mkdir()
            snapshot.gonka_dir.mkdir()
            snapshot.evidence_dir.mkdir()
            harness = root / "a8_acceptance.py"
            harness.write_text("# synthetic harness\n", encoding="utf-8")
            task = get_task_by_id_or_alias("lock-exact-e")
            self.assertIsNotNone(task)

            context = SimpleNamespace(harness_script=harness)
            with patch("ops.a8.adapters.SubprocessRunner") as runner_class:
                runner = runner_class.return_value
                runner.run.return_value = 1
                runner.timed_out = False
                runner.cancelled = False

                status, exit_code, _ = NativeTaskAdapter(task, snapshot, e2e_context=context).execute()

            self.assertEqual(status.value, "FAILED")
            self.assertEqual(exit_code, 1)
            self.assertEqual(runner_class.call_args.kwargs["cwd"], snapshot.marketplace_dir)

    @unittest.skipUnless(os.name == "posix", "symlink race requires the POSIX runner")
    def test_junit_copy_rejects_source_replaced_after_regular_file_check(self):
        with tempfile.TemporaryDirectory(prefix="a8-junit-race-") as temporary:
            root = Path(temporary)
            snapshot = RuntimeSnapshot(root / "runtime", "adapter-junit-001")
            junit_source = snapshot.task_evidence_dir / "testermint-junit"
            junit_source.mkdir(parents=True)
            snapshot.junit_dir.mkdir(parents=True)
            xml = junit_source / "TEST-scenario.xml"
            xml.write_bytes(b"expected JUnit")
            secret = root / "private.txt"
            secret.write_bytes(b"private content")
            original_is_file = Path.is_file
            swapped = False

            def swap_after_check(path):
                nonlocal swapped
                if path == xml and not swapped:
                    assert original_is_file(path)
                    xml.unlink()
                    xml.symlink_to(secret)
                    swapped = True
                    return True
                return original_is_file(path)

            task = get_task_by_id_or_alias("lock-exact-e")
            with patch.object(Path, "is_file", swap_after_check):
                with self.assertRaises(CollectorSecurityError):
                    NativeTaskAdapter(task, snapshot)._collect_junit()
            self.assertTrue(swapped)
            self.assertFalse((snapshot.junit_dir / xml.name).exists())

    def test_junit_copy_retains_a_regular_harness_report(self):
        with tempfile.TemporaryDirectory(prefix="a8-junit-copy-") as temporary:
            snapshot = RuntimeSnapshot(Path(temporary), "adapter-junit-002")
            junit_source = snapshot.task_evidence_dir / "testermint-junit"
            junit_source.mkdir(parents=True)
            snapshot.junit_dir.mkdir(parents=True)
            xml = junit_source / "TEST-scenario.xml"
            xml.write_bytes(b"<testsuite tests='1'/>")
            task = get_task_by_id_or_alias("lock-exact-e")

            NativeTaskAdapter(task, snapshot)._collect_junit()

            self.assertEqual((snapshot.junit_dir / xml.name).read_bytes(), xml.read_bytes())


class BoundaryTaskAdapterDependencyTests(unittest.TestCase):
    def test_contract_tests_use_locked_dependencies_without_forcing_offline_mode(self):
        """A portable source plan must not fail merely because Cargo's cache is empty."""
        for task_id in ("ct-network-unconfirmed", "ct-claim-expiry", "ct-package-c-policy"):
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory(prefix="a8-boundary-") as temporary:
                root = Path(temporary)
                snapshot = RuntimeSnapshot(root, f"{task_id}-001")
                snapshot.run_dir.mkdir(parents=True)
                snapshot.marketplace_dir.mkdir()
                task = get_task_by_id_or_alias(task_id)

                with patch("ops.a8.adapters.SubprocessRunner") as runner_class:
                    runner = runner_class.return_value
                    runner.run.return_value = 0
                    runner.timed_out = False

                    status, exit_code, _ = BoundaryTaskAdapter(task, snapshot).execute()

                argv = runner_class.call_args.kwargs["argv"]
                cargo_args = argv[:argv.index("--")]
                self.assertEqual(status.value, "PASSED")
                self.assertEqual(exit_code, 0)
                self.assertIn("--locked", cargo_args)
                self.assertNotIn("--offline", cargo_args)
                self.assertEqual(runner_class.call_args.kwargs["cwd"], snapshot.marketplace_dir)


class ProcessGroupTests(unittest.TestCase):
    def test_children_are_terminated_after_the_group_leader_has_exited(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SubprocessRunner(["synthetic"], Path(folder), Path(folder) / "log", 10)
            runner.proc = Mock(pid=12345)
            runner.proc.poll.return_value = 0
            runner.pgid = 12345
            with patch("ops.a8.adapters.os.killpg") as killpg, patch(
                "ops.a8.adapters.os.kill", side_effect=ProcessLookupError
            ):
                runner.terminate_own_tree()
                runner.terminate_own_tree()  # Orchestrator cleanup must be idempotent.
            killpg.assert_called_once_with(12345, signal.SIGTERM)
            runner.proc.wait.assert_called_once()

    def test_normal_command_exit_also_cleans_up_the_process_group(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SubprocessRunner(["synthetic"], Path(folder), Path(folder) / "log", 10)
            proc = Mock(pid=12345, stdout=io.BytesIO(b"synthetic output"))
            proc.poll.return_value = 0
            with patch("ops.a8.adapters.subprocess.Popen", return_value=proc), patch(
                "ops.a8.adapters.os.getpgid", return_value=12345
            ), patch("ops.a8.adapters.os.killpg") as killpg, patch(
                "ops.a8.adapters.os.kill", side_effect=ProcessLookupError
            ):
                self.assertEqual(runner.run(), 0)
            killpg.assert_called_once_with(12345, signal.SIGTERM)
            self.assertEqual(runner.log_file.read_bytes(), b"synthetic output")

    def test_a_group_surviving_sigkill_prevents_a_successful_run_result(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SubprocessRunner(["synthetic"], Path(folder), Path(folder) / "log", 100)
            proc = Mock(pid=12345, stdout=io.BytesIO(b"partial evidence"))
            proc.poll.return_value = 0
            with patch("ops.a8.adapters.subprocess.Popen", return_value=proc), patch(
                "ops.a8.adapters.os.getpgid", return_value=12345
            ), patch("ops.a8.adapters.os.killpg") as killpg, patch(
                "ops.a8.adapters.os.kill", return_value=None
            ), patch("ops.a8.adapters.time.monotonic", side_effect=count()), patch(
                "ops.a8.adapters.time.sleep"
            ):
                with self.assertRaises(ProcessExecutionError) as raised:
                    runner.run()
            self.assertEqual(raised.exception.code, "PROCESS_TREE_TERMINATION_FAILED")
            self.assertEqual(raised.exception.exit_code, 1)
            self.assertFalse(runner._tree_terminated)
            self.assertEqual([call.args[1] for call in killpg.call_args_list], [signal.SIGTERM, signal.SIGKILL])
            self.assertEqual(runner.log_file.read_bytes(), b"partial evidence")

    def test_cleanup_failure_does_not_replace_a_keyboard_interruption_with_a_verdict(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SubprocessRunner(["synthetic"], Path(folder), Path(folder) / "log", 100)
            proc = Mock(pid=12345, stdout=io.BytesIO(b"partial evidence"))
            polls = count()
            def poll():
                if next(polls) == 0:
                    raise KeyboardInterrupt
                return 0
            proc.poll.side_effect = poll
            with patch("ops.a8.adapters.subprocess.Popen", return_value=proc), patch(
                "ops.a8.adapters.os.getpgid", return_value=12345
            ), patch("ops.a8.adapters.os.killpg"), patch(
                "ops.a8.adapters.os.kill", return_value=None
            ), patch("ops.a8.adapters.time.monotonic", side_effect=count()), patch(
                "ops.a8.adapters.time.sleep"
            ):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run()
                # A subsequent cleanup attempt still reports the unconfirmed group.
                with self.assertRaises(ProcessExecutionError):
                    runner.terminate_own_tree()
            self.assertTrue(runner.cancelled)
            self.assertEqual(runner.exit_code, 130)

    def test_a_group_disappearing_after_sigkill_is_confirmed_terminated(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = SubprocessRunner(["synthetic"], Path(folder), Path(folder) / "log", 100)
            runner.proc = Mock(pid=12345)
            runner.proc.poll.return_value = 0
            runner.pgid = 12345
            with patch("ops.a8.adapters.os.killpg") as killpg, patch(
                "ops.a8.adapters.os.kill", side_effect=[None, None, ProcessLookupError()]
            ), patch("ops.a8.adapters.time.monotonic", side_effect=count()), patch(
                "ops.a8.adapters.time.sleep"
            ):
                runner.terminate_own_tree()
                runner.terminate_own_tree()
            self.assertTrue(runner._tree_terminated)
            self.assertEqual([call.args[1] for call in killpg.call_args_list], [signal.SIGTERM, signal.SIGKILL])


class WasmTaskBudgetTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "symlink race requires the POSIX runner")
    def test_wasm_copy_rejects_source_replaced_after_regular_file_check(self):
        with tempfile.TemporaryDirectory(prefix="a8-wasm-race-") as temporary:
            root = Path(temporary)
            snapshot = RuntimeSnapshot(root / "runtime", "adapter-wasm-001")
            wasm = snapshot.run_dir / "cargo-target/wasm32-unknown-unknown/release/a8_query_boundary.wasm"
            wasm.parent.mkdir(parents=True)
            wasm.write_bytes(b"expected Wasm")
            secret = root / "private.txt"
            secret.write_bytes(b"private content")
            original_is_file = Path.is_file
            swapped = False

            def swap_after_check(path):
                nonlocal swapped
                if path == wasm and not swapped:
                    assert original_is_file(path)
                    wasm.unlink()
                    wasm.symlink_to(secret)
                    swapped = True
                    return True
                return original_is_file(path)

            task = get_task_by_id_or_alias("wasm-abi-boundary")
            runner = Mock(timed_out=False)
            runner.run.return_value = 0
            with patch.object(Path, "is_file", swap_after_check), patch(
                "ops.a8.adapters.SubprocessRunner", return_value=runner
            ):
                with self.assertRaises(CollectorSecurityError):
                    BoundaryTaskAdapter(task, snapshot)._run_wasm_abi_boundary()
            self.assertTrue(swapped)
            self.assertFalse((snapshot.task_evidence_dir / wasm.name).exists())

    def test_wasm_stages_share_the_catalog_budget_and_report_timeouts(self):
        for stage in ("success", "cargo", "node", "budget"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                snapshot = RuntimeSnapshot(Path(folder), "wasm-budget")
                wasm = snapshot.run_dir / "cargo-target/wasm32-unknown-unknown/release/a8_query_boundary.wasm"
                wasm.parent.mkdir(parents=True)
                wasm.write_bytes(b"synthetic wasm")
                task = get_task_by_id_or_alias("wasm-abi-boundary")
                callback = Mock()
                adapter = BoundaryTaskAdapter(task, snapshot, heartbeat_callback=callback)
                cargo = Mock(timed_out=stage == "cargo")
                cargo.run.return_value = -1 if stage == "cargo" else 0
                node = Mock(timed_out=stage == "node")
                def node_run():
                    (snapshot.task_evidence_dir / "abi.json").write_text("{}")
                    return -1 if stage == "node" else 0
                node.run.side_effect = node_run
                elapsed = task.stage_timeout_seconds + 1 if stage == "budget" else 120
                with patch("ops.a8.adapters.SubprocessRunner", side_effect=[cargo, node]) as factory, patch(
                    "ops.a8.adapters.time.monotonic", side_effect=[10, 10 + elapsed]
                ):
                    status, _, _ = adapter._run_wasm_abi_boundary()
                cargo_argv = factory.call_args_list[0].kwargs["argv"]
                self.assertIn("--locked", cargo_argv)
                self.assertNotIn("--offline", cargo_argv)
                self.assertEqual(status.value, "PASSED" if stage == "success" else "TIMED_OUT")
                self.assertEqual(factory.call_args_list[0].kwargs["timeout_seconds"], task.stage_timeout_seconds)
                self.assertEqual(factory.call_count, 1 if stage in ("cargo", "budget") else 2)
                if factory.call_count == 2:
                    self.assertEqual(factory.call_args_list[1].kwargs["timeout_seconds"], task.stage_timeout_seconds - elapsed)
                    self.assertEqual(
                        factory.call_args_list[1].kwargs["argv"][2],
                        str(snapshot.task_evidence_dir / "a8_query_boundary.wasm"),
                    )
                for call in factory.call_args_list:
                    self.assertIs(call.kwargs["heartbeat_callback"], callback)


if __name__ == "__main__":
    unittest.main()
