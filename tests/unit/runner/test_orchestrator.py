"""Unit tests for SuiteOrchestrator: safe continuation, KeepResources, fixed SHAs.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from forward_e2e.suite.catalog import resolve_e2e_selection
from forward_e2e.execution.cli import cmd_list
from forward_e2e.execution.context import E2ERunContext
from forward_e2e.suite.models import EvidenceStatus, ExecutionStatus, TaskPlan
from forward_e2e.suite.orchestrator import SuiteOrchestrator, compute_runner_hash, generate_suite_id
from forward_e2e.suite.runtime import SuiteRuntimeError, RuntimeSnapshot
from tests.unit.runner.real_fixtures import (
    EVIDENCE_DIR,
    GONKA_SOURCE_SHA,
    GONKA_TREE_SHA,
    MARKETPLACE_SHA,
    MARKETPLACE_TREE_SHA,
    real_lock_exact_e_context_for_run,
    runtime_identity_document,
    write_immutable_companion_files,
    _snapshot_fingerprint,
)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-orch-")
        self.root = Path(self.tmp_dir.name)
        self.marketplace = self.root / "marketplace"
        self.gonka = self.root / "gonka"
        self.output_dir = self.root / "output"
        self.runtime_root = self.root / "runtime"

        self.marketplace.mkdir(parents=True)
        self.gonka.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        self.runtime_root.mkdir(parents=True)

        tree_patcher = patch(
            "forward_e2e.suite.orchestrator.git_tree_sha",
            side_effect=lambda repo: (
                GONKA_TREE_SHA
                if Path(repo).resolve() == self.gonka.resolve()
                else MARKETPLACE_TREE_SHA
            ),
        )
        self.mock_git_tree_sha = tree_patcher.start()
        self.addCleanup(tree_patcher.stop)

        self.e2e_ctx = E2ERunContext(
            plan_id="plan-test",
            contracts_requested_sha=MARKETPLACE_SHA,
            gonka_requested_sha=GONKA_SOURCE_SHA,
            expected_gonka_sha=GONKA_SOURCE_SHA,
            expected_marketplace_sha=MARKETPLACE_SHA,
            evidence_model=E2ERunContext.E2E_EVIDENCE_MODEL,
        )

        self.orch = SuiteOrchestrator(
            marketplace_dir=self.marketplace,
            gonka_dir=self.gonka,
            output_dir=self.output_dir,
            runtime_root=self.runtime_root,
            e2e_context=self.e2e_ctx,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def make_snapshot(self, *, run_id, **kwargs):
        """Model the filesystem boundary without Git, Docker, or toolchain probes."""
        snap = RuntimeSnapshot(self.runtime_root, run_id)
        snap.evidence_dir.mkdir(parents=True)
        snap.junit_dir.mkdir(parents=True)
        snap.identity_file.write_text(
            json.dumps(runtime_identity_document(run_id)), encoding="utf-8"
        )
        return snap

    def run_failure_sequence(self, suite_id, *, status=ExecutionStatus.FAILED,
                             cleanup="CLEANED", terminate_error=None, dirty_sources=False,
                             recorded_violation=False):
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e", "lock-e-plus-4"])
        adapter = MagicMock()
        adapter.execute.return_value = (status, 1, "Synthetic scenario failure")
        if terminate_error:
            adapter.terminate_own_tree.side_effect = RuntimeError(terminate_error)
        self.orch.use_direct_sources = True

        def prepare_boundary(**kwargs):
            snapshot = self.make_snapshot(**kwargs)
            if recorded_violation:
                snapshot.task_evidence_dir.mkdir(parents=True, exist_ok=True)
                (snapshot.task_evidence_dir / "source-immutability.json").write_text(
                    '{"schema":"a8.source-immutability-set/2","verdict":"VIOLATED","roots":{}}'
                )
            return snapshot

        def source_boundary(root):
            gonka = Path(root) == self.gonka
            fingerprint = _snapshot_fingerprint(
                GONKA_SOURCE_SHA if gonka else MARKETPLACE_SHA,
                GONKA_TREE_SHA if gonka else MARKETPLACE_TREE_SHA,
            )
            if dirty_sources and gonka:
                fingerprint["status"] = [" M tracked.rs"]
            return fingerprint

        with patch("forward_e2e.suite.orchestrator.get_git_clean_head", side_effect=[MARKETPLACE_SHA, GONKA_SOURCE_SHA]), patch(
            "forward_e2e.suite.orchestrator.prepare_runtime_snapshot", side_effect=prepare_boundary,
        ), patch("forward_e2e.suite.orchestrator.NativeTaskAdapter", return_value=adapter), patch(
            "forward_e2e.suite.orchestrator.perform_runtime_cleanup", return_value={"status": cleanup},
        ), patch("forward_e2e.suite.orchestrator.capture", side_effect=source_boundary):
            code = self.orch.run_suite(tasks=tasks, suite_id=suite_id)
        result = json.loads((self.output_dir / suite_id / "suite-result.json").read_text())
        return code, result, adapter

    def test_scenario_timeout_is_recorded_and_next_task_runs_with_a_nonzero_final_exit(self):
        code, result, adapter = self.run_failure_sequence("suite-timeout-continue", status=ExecutionStatus.TIMED_OUT)
        self.assertEqual(code, 1)
        self.assertEqual(adapter.execute.call_count, 2)
        self.assertEqual(result["overall_status"], "FAILED")
        self.assertEqual(result["tasks"][0]["raw_execution_status"], "TIMED_OUT")
        self.assertEqual(result["tasks"][0]["primary_failure"], "Synthetic scenario failure")
        self.assertNotEqual(result["tasks"][1]["execution_status"], "NOT_RUN")

    def test_failed_or_unconfirmed_cleanup_prevents_the_next_task_from_starting_and_records_the_reason(self):
        for status in ("FAILED", "UNKNOWN", "KEPT"):
            with self.subTest(status=status):
                code, result, adapter = self.run_failure_sequence(f"suite-cleanup-{status}", cleanup=status)
                self.assertEqual(code, 1)
                self.assertEqual(adapter.execute.call_count, 1)
                self.assertEqual(result["tasks"][1]["execution_status"], "NOT_RUN")
                self.assertIn("Unsafe continuation", result["tasks"][1]["primary_failure"])

    def test_process_tree_termination_failure_prevents_the_next_task_from_starting(self):
        code, result, adapter = self.run_failure_sequence("suite-termination-failed", terminate_error="process still alive")
        self.assertEqual(code, 1)
        self.assertEqual(adapter.execute.call_count, 1)
        self.assertEqual(result["tasks"][1]["execution_status"], "NOT_RUN")
        self.assertTrue(any("process still alive" in error for error in result["tasks"][0]["secondary_errors"]))

    def test_mutation_of_shared_sources_after_a_failed_task_halts_before_the_next_task(self):
        code, result, adapter = self.run_failure_sequence("suite-mutated-sources", dirty_sources=True)
        self.assertEqual(code, 1)
        self.assertEqual(adapter.execute.call_count, 1)
        self.assertEqual(result["tasks"][1]["execution_status"], "NOT_RUN")
        self.assertTrue(any("Unsafe sources" in error for error in result["tasks"][0]["secondary_errors"]))

    def test_a_recorded_source_violation_halts_even_if_the_sources_are_pristine_again(self):
        code, result, adapter = self.run_failure_sequence("suite-recorded-violation", recorded_violation=True)
        self.assertEqual(code, 1)
        self.assertEqual(adapter.execute.call_count, 1)
        self.assertEqual(result["tasks"][1]["execution_status"], "NOT_RUN")
        self.assertTrue(any("Source provenance failure" in error for error in result["tasks"][0]["secondary_errors"]))

    def test_orchestrator_rejects_missing_e2e_context(self):
        with self.assertRaises(ValueError) as cm:
            SuiteOrchestrator(
                marketplace_dir=self.marketplace,
                gonka_dir=self.gonka,
                output_dir=self.output_dir,
                runtime_root=self.runtime_root,
                e2e_context=None,
            )
        self.assertIn("requires an explicit e2e_context", str(cm.exception))

    def test_generated_suite_id_contains_timestamp_and_six_character_safe_suffix(self):
        sid = generate_suite_id()
        self.assertRegex(sid, r"\Asuite-[0-9]{8}-[0-9]{6}-[a-z0-9]{6}\Z")

    def test_public_list_and_selection_resolution_do_not_create_directories(self):
        # Observe directory creation itself: neither function receives output_dir.
        with patch("pathlib.Path.mkdir") as mkdir, patch("os.makedirs") as makedirs:
            buf = io.StringIO()
            self.assertEqual(cmd_list(SimpleNamespace(json=False), out=buf), 0)
            self.assertIn("lock-exact-e", buf.getvalue())
            tasks, profile, _ = resolve_e2e_selection(profile="smoke")
            self.assertEqual([task.task_id for task in tasks], ["lock-exact-e"])
            self.assertEqual(profile, "smoke")
            mkdir.assert_not_called()
            makedirs.assert_not_called()

    def test_empty_or_unknown_tasks_are_rejected_before_git_inspection_without_filesystem_changes(self):
        base_task = resolve_e2e_selection(profile="smoke")[0][0]
        unknown_dict = base_task.to_dict()
        unknown_dict["task_id"] = "unknown-task-xyz"
        unknown_task = TaskPlan.from_dict(unknown_dict)
        initial_paths = set(self.root.rglob("*"))
        for suite_id, tasks in (("suite-empty", []), ("suite-unknown", [unknown_task])):
            with self.subTest(suite_id=suite_id), patch(
                "forward_e2e.suite.orchestrator.get_git_clean_head",
                side_effect=AssertionError("Git inspection must not start"),
            ) as mock_get_head:
                # Exit code 2 alone could be caused by a later Git failure.
                self.assertEqual(self.orch.run_suite(tasks=tasks, suite_id=suite_id), 2)
                mock_get_head.assert_not_called()
                self.assertEqual(set(self.root.rglob("*")), initial_paths)

    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    def test_existing_suite_is_preserved_and_rejected_before_git_inspection(self, mock_get_head):
        existing_sid = "suite-existing-001"
        existing_dir = self.output_dir / existing_sid
        existing_dir.mkdir()
        sentinel = existing_dir / "suite-plan.json"
        sentinel_bytes = b'{"suite_id": "suite-existing-001"}\n'
        sentinel.write_bytes(sentinel_bytes)
        # A later Git failure also returns 2; it must never explain this rejection.
        mock_get_head.side_effect = AssertionError("Git inspection must not start")

        tasks, _, _ = resolve_e2e_selection(profile="smoke")
        code = self.orch.run_suite(
            tasks=tasks,
            profile="smoke",
            suite_id=existing_sid,
        )
        self.assertEqual(code, 2)
        mock_get_head.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
        self.assertEqual(list(existing_dir.iterdir()), [sentinel])

    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.NativeTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    def test_failed_scenario_is_recorded_and_the_next_scenario_runs_after_successful_cleanup(
        self, mock_cleanup, mock_adapter_cls, mock_prepare, mock_get_head, mock_bundle
    ):
        """Scenario failures retain a failed suite verdict without skipping later coverage."""
        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]

        mock_prepare.side_effect = self.make_snapshot

        # Adapter returns failure on first task
        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = (ExecutionStatus.FAILED, 1, "Injected test failure")
        mock_adapter_cls.return_value = mock_adapter

        mock_cleanup.return_value = {"status": "CLEANED"}

        sid = "suite-fail-test"
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e", "lock-e-plus-4"])
        code = self.orch.run_suite(
            tasks=tasks,
            requested_scenarios=["lock-exact-e", "lock-e-plus-4"],
            suite_id=sid,
        )

        self.assertEqual(code, 1)

        # Check suite result
        res_file = self.output_dir / sid / "suite-result.json"
        self.assertTrue(res_file.is_file())
        res_data = json.loads(res_file.read_text(encoding="utf-8"))

        self.assertEqual(res_data["overall_status"], "FAILED")
        res_tasks = res_data["tasks"]
        self.assertEqual(len(res_tasks), 2)
        # Task 1 failed
        self.assertEqual(res_tasks[0]["task_id"], "lock-exact-e")
        self.assertEqual(res_tasks[0]["execution_status"], "FAILED")
        self.assertEqual(mock_adapter.execute.call_count, 2)
        self.assertEqual(mock_cleanup.call_count, 2)
        self.assertEqual(res_tasks[0]["primary_failure"], "Injected test failure")
        # The second failure is independently executed and recorded.
        self.assertEqual(res_tasks[1]["task_id"], "lock-e-plus-4")
        self.assertEqual(res_tasks[1]["execution_status"], "FAILED")
        self.assertIn("TASK_FAILED_CONTINUING", (self.output_dir / sid / "events.jsonl").read_text())

    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.BoundaryTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    @patch("forward_e2e.suite.orchestrator.evaluate_task_evidence")
    @patch("forward_e2e.suite.orchestrator.verify_and_recalculate_suite")
    @patch("forward_e2e.suite.orchestrator.OfflineReporter.write_reports")
    def test_incomplete_checkpoint_list_is_not_hidden_by_complete_flag(
        self,
        mock_reporter_write_reports,
        mock_verify_suite,
        mock_evaluate,
        mock_cleanup,
        mock_adapter_cls,
        mock_prepare,
        mock_get_head,
        mock_bundle,
    ):
        """An inconsistent verifier result must not erase missing predicates."""
        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]

        mock_prepare.side_effect = self.make_snapshot
        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = (ExecutionStatus.PASSED, 0, None)
        mock_adapter_cls.return_value = mock_adapter
        mock_evaluate.return_value = (
            ExecutionStatus.PASSED,
            EvidenceStatus.COMPLETE,
            ["docker_build_completed", "TestStrictPlanValidation_passed", "go_exit_zero"],
            [],
            None,
        )
        mock_cleanup.return_value = {"status": "CLEANED"}
        mock_verify_suite.return_value = MagicMock(
            result=MagicMock(overall_status=ExecutionStatus.PASSED),
            integrity_errors=(),
        )
        mock_reporter_write_reports.return_value = MagicMock(overall_status=ExecutionStatus.PASSED)

        sid = "suite-boundary-complete"
        tasks, _, _ = resolve_e2e_selection(scenarios=["go-query-error-classification"])
        self.orch.run_suite(tasks=tasks, requested_scenarios=["go-query-error-classification"], suite_id=sid)
        task = json.loads(
            (self.output_dir / sid / "suite-result.json").read_text(encoding="utf-8")
        )["tasks"][0]
        self.assertEqual(task["evidence_status"], "COMPLETE")
        self.assertEqual(task["observed_passed_cases"], [
            "docker_build_completed", "TestStrictPlanValidation_passed", "go_exit_zero",
        ])
        self.assertEqual(task["missing_cases"], ["TestToQuerierResultClassifiesVMSystemErrors_passed"])

    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.NativeTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    def test_keep_resources_is_forwarded_to_cleanup_and_stops_after_a_successful_task(
        self, mock_cleanup, mock_adapter_cls, mock_prepare, mock_get_head, mock_bundle
    ):
        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]

        def make_fake_snap(**kwargs):
            snap = self.make_snapshot(**kwargs)

            # Write valid JUnit XML and live-context
            (snap.junit_dir / "TEST-MarketplaceContractAcceptanceTests.xml").write_text("""
            <testsuite><testcase name='marketplace funded lock succeeds exactly at E()'/></testsuite>
            """, encoding="utf-8")

            snap.task_evidence_dir.mkdir()
            (snap.task_evidence_dir / "live-context.json").write_text(
                json.dumps(real_lock_exact_e_context_for_run(snap.run_id)), encoding="utf-8"
            )
            write_immutable_companion_files(snap.task_evidence_dir)
            (snap.evidence_dir / "testermint.log").write_text("ok", encoding="utf-8")

            return snap

        mock_prepare.side_effect = make_fake_snap

        # Task 1 passes
        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = (ExecutionStatus.PASSED, 0, None)
        mock_adapter_cls.return_value = mock_adapter

        mock_cleanup.return_value = {"status": "KEPT"}

        sid = "suite-keep-test"
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e", "lock-e-plus-4"])
        code = self.orch.run_suite(
            tasks=tasks,
            requested_scenarios=["lock-exact-e", "lock-e-plus-4"],
            suite_id=sid,
            keep_resources=True,
        )

        # KeepResources returns 1 (incomplete managed run)
        self.assertEqual(code, 1)

        res_data = json.loads((self.output_dir / sid / "suite-result.json").read_text(encoding="utf-8"))
        res_tasks = res_data["tasks"]
        # KEPT resources remain a failure even when later tasks never ran.
        self.assertEqual(res_data["overall_status"], "FAILED")
        self.assertEqual(len(res_tasks), 2)
        # A failed first task would make NOT_RUN pass for the wrong reason.
        self.assertEqual(res_tasks[0]["execution_status"], "PASSED")
        self.assertEqual(res_tasks[0]["evidence_status"], "COMPLETE")
        self.assertEqual(res_tasks[0]["missing_cases"], [])
        self.assertEqual(res_tasks[0]["cleanup_status"], "KEPT")
        task_result = self.output_dir / sid / "runs" / res_tasks[0]["run_id"] / "result.json"
        artifact_index = json.loads((self.output_dir / sid / "artifact-index.json").read_text(encoding="utf-8"))
        result_entry = next(
            item for item in artifact_index["artifacts"]
            if item["relative_path"] == f"runs/{res_tasks[0]['run_id']}/result.json"
        )
        self.assertEqual(result_entry["sha256"], hashlib.sha256(task_result.read_bytes()).hexdigest())
        self.assertEqual(result_entry["size_bytes"], task_result.stat().st_size)
        mock_cleanup.assert_called_once()
        self.assertIs(mock_cleanup.call_args.kwargs["keep_resources"], True)
        mock_prepare.assert_called_once()
        mock_adapter.execute.assert_called_once()
        # Task 2 was NOT_RUN
        self.assertEqual(res_tasks[1]["execution_status"], "NOT_RUN")

    def test_invalid_suite_ids_are_rejected_before_git_inspection_without_filesystem_changes(self):
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e"])
        initial_paths = set(self.root.rglob("*"))
        # Escape output_dir while keeping any accidental writes inside this test's root.
        for suite_id in ("../escaped-suite", "invalid suite id with spaces!"):
            with self.subTest(suite_id=suite_id), patch(
                "forward_e2e.suite.orchestrator.get_git_clean_head",
                side_effect=AssertionError("Git inspection must not start"),
            ) as mock_get_head:
                self.assertEqual(self.orch.run_suite(tasks=tasks, suite_id=suite_id), 2)
                mock_get_head.assert_not_called()
                self.assertEqual(set(self.root.rglob("*")), initial_paths)

    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.NativeTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    def test_unexpected_exception_preserves_cleanup_and_journal(
        self, mock_cleanup, mock_adapter_cls, mock_prepare, mock_get_head, mock_bundle
    ):
        """Unexpected exception in adapter execution must NOT bypass cleanup, journal, or result recording."""
        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]

        mock_prepare.side_effect = self.make_snapshot

        # Adapter raises unexpected NameError
        mock_adapter = MagicMock()
        mock_adapter.execute.side_effect = NameError("simulated NameError: name 'shutil' is not defined")
        mock_adapter_cls.return_value = mock_adapter

        mock_cleanup.return_value = {"status": "CLEANED"}

        sid = "suite-exc-test"
        tasks, _, _ = resolve_e2e_selection(scenarios=["lock-exact-e"])
        code = self.orch.run_suite(
            tasks=tasks,
            requested_scenarios=["lock-exact-e"],
            suite_id=sid,
        )
        self.assertEqual(code, 1)

        # Verify cleanup was still called!
        mock_cleanup.assert_called_once()

        # Verify result was written!
        res_file = self.output_dir / sid / "suite-result.json"
        self.assertTrue(res_file.is_file())
        res_data = json.loads(res_file.read_text(encoding="utf-8"))
        self.assertEqual(res_data["overall_status"], "FAILED")
        self.assertIn("NameError", res_data["tasks"][0]["primary_failure"])

        # Verify events.jsonl contains TASK_EXCEPTION
        events_file = self.output_dir / sid / "events.jsonl"
        self.assertTrue(events_file.is_file())
        events_text = events_file.read_text(encoding="utf-8")
        self.assertIn("TASK_EXCEPTION", events_text)

    @patch("forward_e2e.suite.orchestrator.OfflineReporter.write_reports")
    @patch("forward_e2e.suite.orchestrator.verify_and_recalculate_suite")
    @patch("forward_e2e.suite.orchestrator.evaluate_task_evidence")
    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.BoundaryTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    def test_run_suite_writes_evidence_directly_to_output_and_keeps_bundles_in_runtime_root(
        self,
        mock_cleanup,
        mock_boundary_cls,
        mock_prepare,
        mock_get_head,
        mock_bundle,
        mock_evaluate,
        mock_verify_suite,
        mock_reporter_write_reports,
    ):
        """Suite evidence is collected directly into output_dir/<suite_id> without intermediate staging or bundles."""
        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]

        def fake_bundle(repo_dir, out_file):
            Path(out_file).write_text("bundle-data", encoding="utf-8")

        mock_bundle.side_effect = fake_bundle
        report_bytes = (EVIDENCE_DIR / "go-query-error-classification/report.json").read_bytes()

        def make_fake_snap(**kwargs):
            snap = self.make_snapshot(**kwargs)
            (snap.evidence_dir / "report.json").write_bytes(report_bytes)
            return snap

        mock_prepare.side_effect = make_fake_snap
        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = (ExecutionStatus.PASSED, 0, None)
        mock_boundary_cls.return_value = mock_adapter
        mock_evaluate.return_value = (
            ExecutionStatus.PASSED,
            EvidenceStatus.COMPLETE,
            ["TestStrictPlanValidation"],
            [],
            None,
        )
        mock_cleanup.return_value = {"status": "CLEANED"}
        mock_verify_suite.return_value = MagicMock(
            result=MagicMock(overall_status=ExecutionStatus.PASSED),
            integrity_errors=(),
        )
        mock_reporter_write_reports.return_value = MagicMock(overall_status=ExecutionStatus.PASSED)

        ws_dir = self.root / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        orch_with_ws = SuiteOrchestrator(
            marketplace_dir=self.marketplace,
            gonka_dir=self.gonka,
            output_dir=self.output_dir,
            runtime_root=self.runtime_root,
            workspace_dir=ws_dir,
            e2e_context=self.e2e_ctx,
        )

        sid = "suite-direct-evidence"
        tasks, _, _ = resolve_e2e_selection(scenarios=["go-query-error-classification"])
        code = orch_with_ws.run_suite(
            tasks=tasks,
            requested_scenarios=["go-query-error-classification"],
            suite_id=sid,
        )
        self.assertEqual(code, 0)

        suite_dir = self.output_dir / sid
        self.assertTrue((suite_dir / "suite-plan.json").is_file())
        self.assertTrue((suite_dir / "suite-result.json").is_file())
        run_id = json.loads((suite_dir / "suite-result.json").read_text(encoding="utf-8"))["tasks"][0]["run_id"]
        # The fake identity is fixed, so inspect the actual handover as well:
        # omitting these arguments would let the runtime select fresh HEADs.
        bundle_dir = self.runtime_root.resolve() / "bundles" / sid
        mock_prepare.assert_called_once_with(
            marketplace_source=self.marketplace.resolve(),
            gonka_source=self.gonka.resolve(),
            run_id=run_id,
            runtime_root=self.runtime_root.resolve(),
            lock=ANY,
            fixed_marketplace_sha=MARKETPLACE_SHA,
            fixed_gonka_sha=GONKA_SOURCE_SHA,
            existing_marketplace_bundle=bundle_dir / "marketplace.bundle",
            existing_gonka_bundle=bundle_dir / "gonka.bundle",
            check_cosmwasm=True,
            recorded_gonka_source_sha=GONKA_SOURCE_SHA,
            evidence_model=self.e2e_ctx.evidence_model,
        )
        snapshot = mock_boundary_cls.call_args.args[1]
        ident = json.loads(snapshot.identity_file.read_text(encoding="utf-8"))
        self.assertEqual(ident["gonka_commit_sha"], GONKA_SOURCE_SHA)
        report_relpath = f"runs/{run_id}/evidence/report.json"
        self.assertEqual((suite_dir / report_relpath).read_bytes(), report_bytes)
        index = json.loads((suite_dir / "artifact-index.json").read_text(encoding="utf-8"))
        entries = [entry for entry in index["artifacts"] if entry["relative_path"] == report_relpath]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sha256"], hashlib.sha256(report_bytes).hexdigest())
        self.assertEqual(entries[0]["size_bytes"], len(report_bytes))
        self.assertEqual(entries[0]["run_id"], run_id)
        self.assertEqual(entries[0]["task_id"], "go-query-error-classification")
        self.assertFalse((suite_dir / "bundles").exists())
        self.assertFalse((ws_dir / "suites" / sid).exists())
        self.assertTrue((self.runtime_root / "bundles" / sid / "marketplace.bundle").is_file())
        self.assertTrue((self.runtime_root / "bundles" / sid / "gonka.bundle").is_file())

    @patch("forward_e2e.suite.orchestrator.OfflineReporter.write_reports")
    @patch("forward_e2e.suite.orchestrator.verify_and_recalculate_suite")
    @patch("forward_e2e.suite.orchestrator.evaluate_task_evidence")
    @patch("forward_e2e.suite.orchestrator.create_git_bundle")
    @patch("forward_e2e.suite.orchestrator.get_git_clean_head")
    @patch("forward_e2e.suite.orchestrator.prepare_runtime_snapshot")
    @patch("forward_e2e.suite.orchestrator.BoundaryTaskAdapter")
    @patch("forward_e2e.suite.orchestrator.perform_runtime_cleanup")
    def test_orchestrator_with_use_direct_sources_skips_bundle_creation_and_preserves_raw_execution_status_on_incomplete_evidence(
        self,
        mock_cleanup,
        mock_boundary_cls,
        mock_prepare,
        mock_get_head,
        mock_bundle,
        mock_evaluate,
        mock_verify_suite,
        mock_reporter_write_reports,
    ):
        from forward_e2e.execution.planner import resolve_e2e_selection

        mock_get_head.side_effect = [MARKETPLACE_SHA, GONKA_SOURCE_SHA]
        mock_prepare.side_effect = self.make_snapshot
        mock_adapter = MagicMock()
        mock_adapter.execute.return_value = (ExecutionStatus.PASSED, 0, None)
        mock_boundary_cls.return_value = mock_adapter
        mock_evaluate.return_value = (
            ExecutionStatus.FAILED,
            EvidenceStatus.INCOMPLETE,
            [],
            ["Missing required artifact report.json"],
            "Missing required artifact report.json",
        )
        mock_cleanup.return_value = {"status": "CLEANED"}
        mock_verify_suite.return_value = MagicMock(
            result=MagicMock(overall_status=ExecutionStatus.FAILED),
            integrity_errors=(),
        )
        mock_reporter_write_reports.return_value = MagicMock(overall_status=ExecutionStatus.FAILED)

        ws_dir = self.root / "workspace_direct"
        ws_dir.mkdir(parents=True, exist_ok=True)
        orch_direct = SuiteOrchestrator(
            marketplace_dir=self.marketplace,
            gonka_dir=self.gonka,
            output_dir=self.output_dir,
            runtime_root=self.runtime_root,
            workspace_dir=ws_dir,
            e2e_context=self.e2e_ctx,
            use_direct_sources=True,
        )

        sid = "suite-direct-sources"
        tasks, _, _ = resolve_e2e_selection(scenarios=["go-query-error-classification"])
        code = orch_direct.run_suite(
            tasks=tasks,
            requested_scenarios=["go-query-error-classification"],
            suite_id=sid,
        )
        self.assertEqual(code, 1)
        mock_bundle.assert_not_called()
        self.assertFalse((self.runtime_root / "bundles" / sid).exists())
        self.assertTrue(mock_prepare.call_args.kwargs["use_direct_sources"])

        suite_res = json.loads((self.output_dir / sid / "suite-result.json").read_text(encoding="utf-8"))
        task_res = suite_res["tasks"][0]
        self.assertEqual(task_res["execution_status"], ExecutionStatus.FAILED.value)
        self.assertEqual(task_res["raw_execution_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(task_res["evidence_status"], EvidenceStatus.INCOMPLETE.value)


class RunnerSourceHashTests(unittest.TestCase):
    def test_python_source_symlink_cannot_read_bytes_outside_runner(self):
        with tempfile.TemporaryDirectory(prefix="a8-hash-") as tmp:
            root = Path(tmp)
            source = root / "ops" / "a8"
            source.mkdir(parents=True)
            (source / "core.py").write_text("VALUE = 1\n")
            outside = root / "outside.py"
            outside.write_text("VALUE = 2\n")
            (source / "linked.py").symlink_to(outside)
            with self.assertRaises(SuiteRuntimeError):
                compute_runner_hash(source)

    def test_python_source_directory_symlink_cannot_disappear_from_runner_hash(self):
        with tempfile.TemporaryDirectory(prefix="a8-hash-") as tmp:
            root = Path(tmp)
            source = root / "ops" / "a8"
            source.mkdir(parents=True)
            (source / "core.py").write_text("VALUE = 1\n")
            external = root / "external"
            external.mkdir()
            (external / "judge.py").write_text("VALUE = 2\n")
            (source / "linked_package").symlink_to(external, target_is_directory=True)
            with self.assertRaises(SuiteRuntimeError):
                compute_runner_hash(source)


if __name__ == "__main__":
    unittest.main()
