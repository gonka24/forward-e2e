"""Unit tests for offline reporting, atomic file writes, and integrity verification.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from dataclasses import replace
import json
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.suite import reporter as reporter_module
from forward_e2e.suite.collector import CollectorSecurityError
from forward_e2e.suite.catalog import get_task_by_id
from forward_e2e.execution.errors import PathSafetyError
from forward_e2e.suite.models import (
    make_task_run_id,
    AcceptanceStatus,
    CleanupStatus,
    EvidenceStatus,
    ExecutionStatus,
    ProofLevel,
    SourceIdentity,
    SuitePlan,
    SuiteResult,
    TaskPlan,
    TaskResult,
)
from forward_e2e.suite.reporter import OfflineReporter, atomic_write_text
from forward_e2e.suite.runtime import sha256_file
from forward_e2e.suite.verifier import (
    load_or_reconstruct_suite_result,
    verify_and_recalculate_suite,
    verify_suite_artifacts_integrity,
)
from tests.unit.runner.real_fixtures import (
    GONKA_SOURCE_SHA,
    GONKA_TREE_SHA,
    LOCK_EXACT_E_CHECKPOINTS,
    MARKETPLACE_SHA,
    MARKETPLACE_TREE_SHA,
    real_lock_exact_e_context_for_run,
    runtime_identity_document,
    write_immutable_companion_files,
)


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-reporter-")
        self.suite_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_atomic_write_text_creates_file(self):
        target = self.suite_dir / "test_file.txt"
        atomic_write_text(target, "hello atomic world")
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "hello atomic world")

    def test_new_reports_are_readable_and_rewrites_preserve_existing_permissions(self):
        for name in ("summary.md", "coverage.json", "suite-result.json"):
            with self.subTest(name=name):
                target = self.suite_dir / name
                atomic_write_text(target, "initial")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                for mode in (0o644, 0o640, 0o600):
                    with self.subTest(mode=oct(mode)):
                        target.chmod(mode)
                        atomic_write_text(target, "updated")
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
                        self.assertEqual(target.read_text(), "updated")

    def test_report_symlinks_are_rejected_without_overwriting_the_external_target(self):
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / "protected"
            target.write_bytes(b"original")
            for name in ("summary.md", "coverage.json", "suite-result.json"):
                with self.subTest(name=name):
                    link = self.suite_dir / name
                    link.symlink_to(target)
                    with self.assertRaises(CollectorSecurityError):
                        atomic_write_text(link, "replacement")
                    self.assertEqual(target.read_bytes(), b"original")
                    self.assertTrue(link.is_symlink())

    def test_a_linked_report_parent_is_rejected_without_writing_outside_the_suite(self):
        with tempfile.TemporaryDirectory() as external:
            parent = self.suite_dir / "linked"
            parent.symlink_to(Path(external), target_is_directory=True)
            with self.assertRaises(CollectorSecurityError):
                atomic_write_text(parent / "summary.md", "replacement")
            self.assertEqual(list(Path(external).iterdir()), [])

    def test_report_parent_swapped_after_path_check_cannot_redirect_write(self):
        """A late parent symlink must not redirect an atomic report outside the suite."""
        parent = self.suite_dir / "owned"
        parent.mkdir()
        held = self.suite_dir / "held-owned"
        with tempfile.TemporaryDirectory(prefix="a8-external-report-") as external:
            outside = Path(external)
            original_safe = reporter_module.is_path_safe
            swapped = False

            def swap_after_check(root, path):
                nonlocal swapped
                safe = original_safe(root, path)
                if safe and not swapped:
                    parent.rename(held)
                    parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return safe

            with patch("forward_e2e.suite.reporter.is_path_safe", side_effect=swap_after_check):
                with self.assertRaises((CollectorSecurityError, PathSafetyError)):
                    atomic_write_text(parent / "summary.md", "must stay inside")
            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])

    def test_failed_report_replacement_preserves_the_original_and_removes_the_temporary_file(self):
        target = self.suite_dir / "summary.md"
        atomic_write_text(target, "original")
        with patch("forward_e2e.execution.file_safety.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                atomic_write_text(target, "replacement")
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(list(self.suite_dir.iterdir()), [target])

    def _write_passed_suite(self, suite_id: str) -> Path:
        """Stage and verify the positive baseline before any negative mutation."""
        source_id = SourceIdentity(
            MARKETPLACE_SHA,
            GONKA_SOURCE_SHA,
            "a" * 64,
            "c" * 64,
            gonka_tree_sha=GONKA_TREE_SHA,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            source_immutability_verdict="UNCHANGED",
        )
        task = TaskPlan(
            task_id="lock-exact-e",
            ordinal=1,
            proof_level=ProofLevel.NATIVE,
            description="Funded lock succeeds exactly at E",
            scenario_selector="lock-exact-e",
            timeout_minutes=45,
            stage_timeout_seconds=2700,
            expected_artifacts=["live-context.json"],
            expected_checkpoints=list(LOCK_EXACT_E_CHECKPOINTS),
            coverage_ids=["C1 exact E lower boundary"],
            limitations=["live network run"],
            evidence_scopes=["lock-exact-e"],
        )
        plan = SuitePlan("1.0.0", suite_id, "2026-09-11T12:00:00Z", "smoke", None, source_id, [task])
        (self.suite_dir / "suite-plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")

        # Recorded business evidence with a synthetic current-runtime envelope.
        run_id = make_task_run_id(suite_id, task.ordinal, task.task_id)
        run_dir = self.suite_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        raw_ctx = run_dir / "live-context.json"
        raw_ctx.write_text(json.dumps(real_lock_exact_e_context_for_run(run_id)), encoding="utf-8")
        companion_files = write_immutable_companion_files(run_dir)

        ident_file = run_dir / "identity.json"
        ident_file.write_text(json.dumps(runtime_identity_document(run_id)), encoding="utf-8")

        junit_file = run_dir / "junit" / "TEST-MarketplaceContractAcceptanceTests.xml"
        junit_file.parent.mkdir()
        junit_file.write_text(
            '<testsuite tests="1"><testcase classname="MarketplaceContractAcceptanceTests" '
            'name="lock-exact-e" /></testsuite>', encoding="utf-8"
        )

        indexed_relpaths = [
            "identity.json",
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            *sorted(companion_files.keys()),
        ]
        artifacts = []
        for rel in indexed_relpaths:
            fpath = run_dir / rel
            artifacts.append(
                {
                    "relative_path": f"runs/{run_id}/{rel}",
                    "size_bytes": fpath.stat().st_size,
                    "sha256": sha256_file(fpath),
                    "run_id": run_id,
                    "task_id": "lock-exact-e",
                    "artifact_kind": "JUNIT" if rel.startswith("junit/") else "METADATA_JSON",
                }
            )

        # Run task result
        task_res = TaskResult(
            task_id="lock-exact-e",
            ordinal=1,
            run_id=run_id,
            proof_level=ProofLevel.NATIVE,
            execution_status=ExecutionStatus.PASSED,
            evidence_status=EvidenceStatus.COMPLETE,
            cleanup_status=CleanupStatus.CLEANED,
            acceptance_status=AcceptanceStatus.NOT_REVIEWED,
            start_time_utc="2026-09-11T12:00:05Z",
            end_time_utc="2026-09-11T12:08:00Z",
            duration_seconds=475.0,
            phase="COMPLETED",
            exit_code=0,
            primary_failure=None,
            secondary_errors=[],
            expected_cases=list(LOCK_EXACT_E_CHECKPOINTS),
            observed_passed_cases=list(LOCK_EXACT_E_CHECKPOINTS),
            missing_cases=[],
            missing_artifacts=[],
            raw_evidence_dir="/tmp/fake",
            exported_evidence_dir=str(run_dir),
        )
        result_path = run_dir / "result.json"
        result_path.write_text(json.dumps(task_res.to_dict()), encoding="utf-8")
        artifacts.append({
            "relative_path": f"runs/{run_id}/result.json",
            "size_bytes": result_path.stat().st_size,
            "sha256": sha256_file(result_path),
            "run_id": run_id,
            "task_id": task.task_id,
            "artifact_kind": "METADATA_JSON",
        })
        index_doc = {
            "schema_version": "1.0.0",
            "total_artifacts": len(artifacts),
            "artifacts": artifacts,
        }
        (self.suite_dir / "artifact-index.json").write_text(json.dumps(index_doc), encoding="utf-8")

        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertEqual(verification.integrity_errors, ())
        self.assertEqual(verification.result.overall_status, ExecutionStatus.PASSED)
        self.assertEqual(verification.result.tasks[0].evidence_status, EvidenceStatus.COMPLETE)
        (self.suite_dir / "suite-result.json").write_text(
            json.dumps(verification.result.to_dict()), encoding="utf-8"
        )
        return run_dir

    def _remove_artifact_and_index_entry(self, artifact: Path) -> None:
        # Remove the evidence fact from both representations so a stale checksum
        # cannot mask the mandatory-artifact guard this test is meant to exercise.
        relative_path = artifact.relative_to(self.suite_dir).as_posix()
        artifact.unlink()
        index_file = self.suite_dir / "artifact-index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        index["artifacts"] = [
            entry for entry in index["artifacts"]
            if entry["relative_path"] != relative_path
        ]
        index["total_artifacts"] = len(index["artifacts"])
        index_file.write_text(json.dumps(index), encoding="utf-8")

    def test_offline_report_does_not_modify_raw_artifacts(self):
        run_dir = self._write_passed_suite("suite-test-01")
        raw_hashes = {path: sha256_file(path) for path in run_dir.rglob("*") if path.is_file()}

        # Verify suite and render reports via OfflineReporter
        verification = verify_and_recalculate_suite(self.suite_dir)
        reporter = OfflineReporter(self.suite_dir)
        res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )

        self.assertEqual(res.overall_status, ExecutionStatus.PASSED)
        self.assertTrue((self.suite_dir / "summary.md").is_file())
        coverage = json.loads((self.suite_dir / "coverage.json").read_text(encoding="utf-8"))
        historical = {row["id"] for row in coverage["historical_only_matrix_rows"]}
        self.assertTrue(historical.isdisjoint({"B1", "C1 missing/mismatch", "F1/F2"}))
        self.assertIn("C1 E+5 Refund / R1", historical)

        # Every raw run artifact must survive reporting byte-for-byte.
        self.assertEqual(
            raw_hashes,
            {path: sha256_file(path) for path in run_dir.rglob("*") if path.is_file()},
        )

        # Check that summary.md contains NOT_REVIEWED authority notice
        summary_text = (self.suite_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("acceptance_status: NOT_REVIEWED", summary_text)
        self.assertIn("Acceptance Authority Notice", summary_text)

    def test_summary_markdown_opens_with_the_runner_neutral_heading_and_the_suite_id(self):
        # The heading is the one line of summary.md that humans and greps key
        # on, so it is pinned exactly; the historical "A8 Test Suite Summary"
        # wording must not come back with a careless merge.
        self._write_passed_suite("suite-heading-01")
        verification = verify_and_recalculate_suite(self.suite_dir)
        OfflineReporter(self.suite_dir).write_reports(
            verification.result, integrity_errors=verification.integrity_errors,
        )
        summary_lines = (self.suite_dir / "summary.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(summary_lines[0], "# Forward E2E Suite Summary: suite-heading-01")
        self.assertNotIn("A8 Test Suite Summary", "\n".join(summary_lines))

    def test_offline_report_reconstructs_interrupted_suite(self):
        self._write_two_task_suite("suite-interrupted", ExecutionStatus.NOT_RUN)
        # Simulate an interruption before the aggregate result was persisted;
        # the completed first task must be recovered from its own result.json.
        (self.suite_dir / "suite-result.json").unlink()

        verification = verify_and_recalculate_suite(self.suite_dir)
        reporter = OfflineReporter(self.suite_dir)
        res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )

        # Overall status must be INTERRUPTED, never false PASS
        self.assertEqual(res.overall_status, ExecutionStatus.INTERRUPTED)
        self.assertEqual(verification.integrity_errors, ())
        self.assertIsNone(verification.stored_result)
        self.assertEqual(len(res.tasks), 2)
        self.assertEqual(res.tasks[0].task_id, "lock-exact-e")
        self.assertEqual(res.tasks[0].execution_status, ExecutionStatus.PASSED)
        self.assertEqual(res.tasks[0].evidence_status, EvidenceStatus.COMPLETE)
        self.assertEqual(res.tasks[0].observed_passed_cases, list(LOCK_EXACT_E_CHECKPOINTS))
        self.assertEqual(res.tasks[1].task_id, "lock-e-plus-4")
        self.assertEqual(res.tasks[1].execution_status, ExecutionStatus.NOT_RUN)

    def test_offline_report_does_not_create_missing_final_suite_result(self):
        """Rendering derived reports must not manufacture a missing final record."""
        self._write_passed_suite("suite-missing-final-result")
        result_path = self.suite_dir / "suite-result.json"
        result_path.unlink()

        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertIsNone(verification.stored_result)
        OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
            write_suite_result=False,
        )
        self.assertFalse(result_path.exists())

    def test_corrupt_artifact_downgrades_status_to_failed(self):
        """P1-8 repro: altering an indexed evidence file must downgrade overall status to FAILED."""
        run_dir = self._write_passed_suite("suite-corrupt-test")
        raw_ctx = run_dir / "live-context.json"
        # JSON whitespace changes only the indexed bytes, leaving all semantic
        # evidence valid. A failure must therefore depend on checksum integrity.
        raw_ctx.write_text(raw_ctx.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        # Re-verify and re-generate the report offline.
        verification = verify_and_recalculate_suite(self.suite_dir)
        reporter = OfflineReporter(self.suite_dir)
        final_res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )

        self.assertTrue(any(
            f"Checksum mismatch for runs/{run_dir.name}/live-context.json:" in error
            for error in verification.integrity_errors
        ), verification.integrity_errors)
        # Overall status MUST be downgraded to FAILED!
        self.assertEqual(final_res.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(final_res.tasks[0].raw_execution_status, ExecutionStatus.PASSED)

        # summary.md must reflect integrity violation
        summary_md = (self.suite_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Integrity and Corruption Violations", summary_md)
        self.assertIn("Checksum mismatch", summary_md)
        self.assertIn("- Test execution: `PASSED`; evidence: `INVALID`", summary_md)
        self.assertIn("- Graded suite status: `FAILED`", summary_md)

    def test_missing_artifact_downgrades_status_to_failed(self):
        run_dir = self._write_passed_suite("suite-missing-test")
        log = run_dir / "launcher.log"
        log.write_text("Synthetic launcher output\n", encoding="utf-8")
        index_file = self.suite_dir / "artifact-index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        relative_path = log.relative_to(self.suite_dir).as_posix()
        index["artifacts"].append({
            "relative_path": relative_path,
            "size_bytes": log.stat().st_size,
            "sha256": sha256_file(log),
            "run_id": run_dir.name,
            "task_id": "lock-exact-e",
            "artifact_kind": "LOG",
        })
        index["total_artifacts"] = len(index["artifacts"])
        index_file.write_text(json.dumps(index), encoding="utf-8")
        baseline = verify_and_recalculate_suite(self.suite_dir)
        self.assertEqual(baseline.integrity_errors, ())
        self.assertEqual(baseline.result.overall_status, ExecutionStatus.PASSED)
        log.unlink()

        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertEqual(verification.integrity_errors, (f"Missing file: {relative_path}",))
        res = OfflineReporter(self.suite_dir).write_reports(
            verification.result, integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(res.overall_status, ExecutionStatus.FAILED)
        summary_md = (self.suite_dir / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"Missing file: {relative_path}", summary_md)

    def test_deletion_of_mandatory_live_context_and_index_entry_downgrades_previously_passed_suite(self):
        """P1-Issue 5 repro: deleting live-context.json and removing its index entry must downgrade PASSED to FAILED."""
        run_dir = self._write_passed_suite("suite-issue5-test")
        self._remove_artifact_and_index_entry(run_dir / "live-context.json")

        # Re-verifying and re-generating report must detect missing live-context.json and downgrade overall status to FAILED
        verification = verify_and_recalculate_suite(self.suite_dir)
        reporter = OfflineReporter(self.suite_dir)
        final_res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertIn(
            f"Task lock-exact-e run {run_dir.name} missing mandatory artifact on disk: live-context.json",
            verification.integrity_errors,
        )
        self.assertEqual(final_res.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(final_res.tasks[0].evidence_status, EvidenceStatus.INVALID)
        self.assertEqual(final_res.tasks[0].execution_status, ExecutionStatus.FAILED)

    def test_missing_identity_file_downgrades_previously_passed_suite(self):
        """P1-Issue 5 repro: missing identity.json must downgrade PASSED to FAILED."""
        run_dir = self._write_passed_suite("suite-no-ident-test")
        self._remove_artifact_and_index_entry(run_dir / "identity.json")

        verification = verify_and_recalculate_suite(self.suite_dir)
        reporter = OfflineReporter(self.suite_dir)
        final_res = reporter.write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(verification.integrity_errors, (
            f"Run {run_dir.name} missing mandatory identity.json on disk",
        ))
        self.assertEqual(final_res.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(final_res.tasks[0].evidence_status, EvidenceStatus.INVALID)

    def _write_two_task_suite(self, suite_id: str, second_status: ExecutionStatus) -> SuitePlan:
        """Stages a two-task suite where the first task is complete on disk."""
        run_dir = self._write_passed_suite(suite_id)
        plan_file = self.suite_dir / "suite-plan.json"
        plan = SuitePlan.from_dict(json.loads(plan_file.read_text(encoding="utf-8")))
        second_task = get_task_by_id("lock-e-plus-4")
        self.assertIsNotNone(second_task)
        plan.tasks.append(replace(second_task, ordinal=2))
        plan_file.write_text(json.dumps(plan.to_dict()), encoding="utf-8")

        first_result = TaskResult.from_dict(
            json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        )
        task = plan.tasks[1]
        passed = second_status == ExecutionStatus.PASSED
        second_result = TaskResult(
            task_id=task.task_id,
            ordinal=task.ordinal,
            run_id=make_task_run_id(suite_id, task.ordinal, task.task_id),
            proof_level=task.proof_level,
            execution_status=second_status,
            evidence_status=EvidenceStatus.COMPLETE if passed else EvidenceStatus.NOT_COLLECTED,
            cleanup_status=CleanupStatus.CLEANED if passed else CleanupStatus.NOT_NEEDED,
            acceptance_status=AcceptanceStatus.NOT_REVIEWED,
            start_time_utc="2026-09-11T12:08:00Z" if passed else None,
            end_time_utc="2026-09-11T12:10:00Z" if passed else None,
            duration_seconds=120.0 if passed else None,
            phase="COMPLETED" if passed else "NOT_STARTED",
            exit_code=0 if passed else None,
            primary_failure=None,
            secondary_errors=[],
            expected_cases=list(task.expected_checkpoints),
            observed_passed_cases=list(task.expected_checkpoints) if passed else [],
            missing_cases=[] if passed else list(task.expected_checkpoints),
            missing_artifacts=[],
            raw_evidence_dir=None,
            exported_evidence_dir=None,
        )
        suite_res = SuiteResult(
            schema_version="1.0.0",
            suite_id=suite_id,
            created_at_utc="2026-09-11T12:00:00Z",
            completed_at_utc="2026-09-11T12:10:00Z",
            source_identity=plan.source_identity,
            overall_status=ExecutionStatus.PASSED
            if second_status == ExecutionStatus.PASSED
            else ExecutionStatus.INTERRUPTED,
            tasks=[first_result, second_result],
            summary_message="Initial run",
        )
        (self.suite_dir / "suite-result.json").write_text(json.dumps(suite_res.to_dict()), encoding="utf-8")
        return plan

    def test_failed_preparation_with_absent_unproduced_artifacts_keeps_previous_complete_evidence_and_fails_the_suite(self):
        plan = self._write_two_task_suite("suite-preparation-failed", ExecutionStatus.NOT_RUN)
        suite_path = self.suite_dir / "suite-result.json"
        stored = json.loads(suite_path.read_text())
        failed = stored["tasks"][1]
        failed.update(
            execution_status="FAILED", evidence_status="INCOMPLETE",
            phase="PREPARING", exit_code=1,
            start_time_utc="2026-09-11T12:08:00Z",
            end_time_utc="2026-09-11T12:08:30Z", duration_seconds=30.0,
            primary_failure="wrapper distribution download timed out",
            missing_artifacts=list(plan.tasks[1].expected_artifacts),
        )
        stored["overall_status"] = "FAILED"
        suite_path.write_text(json.dumps(stored))
        run_dir = self.suite_dir / "runs" / failed["run_id"]
        run_dir.mkdir()
        (run_dir / "identity.json").write_text(json.dumps(runtime_identity_document(failed["run_id"])))
        (run_dir / "result.json").write_text(json.dumps(failed))
        index_path = self.suite_dir / "artifact-index.json"
        index = json.loads(index_path.read_text())
        for name in ("identity.json", "result.json"):
            path = run_dir / name
            index["artifacts"].append({
                "relative_path": path.relative_to(self.suite_dir).as_posix(),
                "sha256": sha256_file(path), "size_bytes": path.stat().st_size,
                "task_id": failed["task_id"], "run_id": failed["run_id"],
                "artifact_kind": "METADATA_JSON",
            })
        index["total_artifacts"] = len(index["artifacts"])
        index_path.write_text(json.dumps(index))
        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertEqual(verification.integrity_errors, ())
        self.assertEqual(verification.result.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(verification.result.tasks[0].execution_status, ExecutionStatus.PASSED)
        self.assertEqual(verification.result.tasks[0].evidence_status, EvidenceStatus.COMPLETE)
        self.assertEqual(verification.result.tasks[1].evidence_status, EvidenceStatus.INCOMPLETE)

        # A declared file disappearing is still corruption even on a failed task.
        (run_dir / "result.json").unlink()
        corrupted = verify_and_recalculate_suite(self.suite_dir)
        self.assertTrue(corrupted.integrity_errors)
        self.assertEqual(corrupted.result.overall_status, ExecutionStatus.FAILED)

    def test_deleted_run_directory_of_executed_task_is_reported(self):
        """Round 4 issue 6: a whole deleted run directory must not be skipped.

        The second task is recorded as PASSED but its run directory and index
        entries are absent, which must invalidate the stale COMPLETE result.
        """
        plan = self._write_two_task_suite("suite-deleted-run", ExecutionStatus.PASSED)

        result = load_or_reconstruct_suite_result(self.suite_dir)
        _, _, errors = verify_suite_artifacts_integrity(self.suite_dir, plan=plan, result=result)
        self.assertTrue(
            any("run directory is missing" in e for e in errors),
            f"Expected a missing run directory error, got: {errors}",
        )

        verification = verify_and_recalculate_suite(self.suite_dir)
        final_res = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(final_res.overall_status, ExecutionStatus.FAILED)
        second = next(t for t in final_res.tasks if t.task_id == "lock-e-plus-4")
        self.assertEqual(second.evidence_status, EvidenceStatus.INVALID)

    def test_task_that_never_ran_does_not_require_a_run_directory(self):
        """A NOT_RUN task has no run directory, and that is not corruption."""
        plan = self._write_two_task_suite("suite-not-run", ExecutionStatus.NOT_RUN)

        result = load_or_reconstruct_suite_result(self.suite_dir)
        _, _, errors = verify_suite_artifacts_integrity(self.suite_dir, plan=plan, result=result)
        self.assertEqual(errors, [])

    def _replace_document_run_id(self, artifact: Path, run_id: str) -> None:
        # Keep the index checksum correct: the negative fact is the document's
        # run binding, not byte corruption or an unrelated missing index entry.
        document = json.loads(artifact.read_text(encoding="utf-8"))
        document["run_id"] = run_id
        artifact.write_text(json.dumps(document), encoding="utf-8")
        index_file = self.suite_dir / "artifact-index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        relative_path = artifact.relative_to(self.suite_dir).as_posix()
        entry = next(item for item in index["artifacts"] if item["relative_path"] == relative_path)
        entry["sha256"] = sha256_file(artifact)
        entry["size_bytes"] = artifact.stat().st_size
        index_file.write_text(json.dumps(index), encoding="utf-8")

    def test_foreign_identity_run_id_is_rejected_when_live_context_matches_the_plan(self):
        run_dir = self._write_passed_suite("suite-foreign-identity")
        foreign_run_id = "suite-other-01-lock-exact-e"
        self._replace_document_run_id(run_dir / "identity.json", foreign_run_id)

        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertEqual(verification.integrity_errors, (
            f"Run {run_dir.name} identity run_id {foreign_run_id!r} does not match "
            f"the plan-derived run id {run_dir.name!r}",
        ))
        result = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(result.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].execution_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].evidence_status, EvidenceStatus.INVALID)

    def test_foreign_live_context_run_id_is_rejected_when_identity_matches_the_plan(self):
        run_dir = self._write_passed_suite("suite-foreign-context")
        foreign_run_id = "suite-other-01-lock-exact-e"
        self._replace_document_run_id(run_dir / "live-context.json", foreign_run_id)

        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertIn(
            f"Task lock-exact-e structured evidence invalid in runs/{run_dir.name}/live-context.json: "
            f"live-context.json run_id {foreign_run_id!r} does not match expected {run_dir.name!r}",
            verification.integrity_errors,
        )
        result = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(result.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].execution_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].evidence_status, EvidenceStatus.INVALID)

    def test_foreign_but_consistent_run_is_rejected(self):
        """A context+identity pair from another run of the same task must fail."""
        run_dir = self._write_passed_suite("suite-foreign-pair")
        foreign_run_id = "suite-other-01-lock-exact-e"
        for name in ("identity.json", "live-context.json"):
            self._replace_document_run_id(run_dir / name, foreign_run_id)
        verification = verify_and_recalculate_suite(self.suite_dir)
        result = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(result.overall_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].execution_status, ExecutionStatus.FAILED)
        self.assertEqual(result.tasks[0].evidence_status, EvidenceStatus.INVALID)

    def test_inexactly_named_run_directory_is_not_accepted(self):
        """The suffix/ordinal fallback match must no longer rescue a task."""
        run_dir = self._write_passed_suite("suite-inexact-directory")
        inexact_dir = run_dir.with_name("suite-inexact-directory-01-lock-exact")
        run_dir.rename(inexact_dir)
        # Reflect the moved files in the index, so rejection proves the run
        # directory must belong to the plan rather than merely exist on disk.
        index_file = self.suite_dir / "artifact-index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        for entry in index["artifacts"]:
            entry["relative_path"] = (
                Path("runs")
                / inexact_dir.name
                / Path(entry["relative_path"]).relative_to(f"runs/{run_dir.name}")
            ).as_posix()
            entry["run_id"] = inexact_dir.name
        index_file.write_text(json.dumps(index), encoding="utf-8")
        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertIn(
            f"Task lock-exact-e was executed but its run directory is missing: runs/{run_dir.name}",
            verification.integrity_errors,
        )
        result = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(result.overall_status, ExecutionStatus.FAILED)

    def test_index_entry_must_agree_with_its_run_directory(self):
        run_dir = self._write_passed_suite("suite-foreign-index")
        index_file = self.suite_dir / "artifact-index.json"
        index = json.loads(index_file.read_text(encoding="utf-8"))
        # Change only one entry, leaving both documents bound to the right run.
        entry = index["artifacts"][0]
        entry["run_id"] = "suite-other-01-lock-exact-e"
        index_file.write_text(json.dumps(index), encoding="utf-8")
        verification = verify_and_recalculate_suite(self.suite_dir)
        self.assertIn(
            f"Artifact {entry['relative_path']} records run_id {entry['run_id']!r} "
            f"but is stored under {run_dir.name!r}",
            verification.integrity_errors,
        )
        result = OfflineReporter(self.suite_dir).write_reports(
            verification.result,
            integrity_errors=verification.integrity_errors,
        )
        self.assertEqual(result.overall_status, ExecutionStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
