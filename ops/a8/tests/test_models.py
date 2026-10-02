"""Suite outcome and timeout regressions using producer-shaped fixtures.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ops.a8.catalog import get_task_by_id_or_alias
from ops.a8.models import (
    CleanupStatus, EvidenceStatus, ExecutionStatus, SuiteResult, TaskPlan,
    calculate_suite_outcome,
)
from ops.a8.tests.support.fakes import write_suite_output


class SuiteOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        suite_dir = write_suite_output(
            suite_dir=Path(temporary.name), suite_id="model-review",
            scenarios=["lock-exact-e"],
        )
        self.task = SuiteResult.from_dict(json.loads(
            (suite_dir / "suite-result.json").read_text()
        )).tasks[0]
        self.assertEqual(calculate_suite_outcome([self.task]), (ExecutionStatus.PASSED, 0))

    def test_non_successful_execution_states_never_receive_a_passing_verdict(self):
        expected = {
            ExecutionStatus.NOT_RUN: ExecutionStatus.INTERRUPTED,
            ExecutionStatus.RUNNING: ExecutionStatus.INTERRUPTED,
            ExecutionStatus.INTERRUPTED: ExecutionStatus.INTERRUPTED,
            ExecutionStatus.CANCELLED: ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED: ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT: ExecutionStatus.TIMED_OUT,
        }
        for status, outcome in expected.items():
            with self.subTest(status=status):
                changed = replace(self.task, execution_status=status)
                self.assertEqual(calculate_suite_outcome([changed]), (
                    outcome, 130 if outcome == ExecutionStatus.CANCELLED else 1,
                ))

    def test_success_requires_confirmed_or_unnecessary_cleanup(self):
        for cleanup in CleanupStatus:
            with self.subTest(cleanup=cleanup):
                valid = cleanup in (CleanupStatus.CLEANED, CleanupStatus.NOT_NEEDED)
                self.assertEqual(calculate_suite_outcome([replace(self.task, cleanup_status=cleanup)]), (
                    ExecutionStatus.PASSED if valid else ExecutionStatus.FAILED, 0 if valid else 1,
                ))

    def test_pending_tasks_do_not_mask_completed_evidence_cleanup_or_export_failures(self):
        pending = replace(self.task, execution_status=ExecutionStatus.NOT_RUN)
        mutations = (
            {"evidence_status": EvidenceStatus.INCOMPLETE},
            {"evidence_status": EvidenceStatus.INVALID},
            {"cleanup_status": CleanupStatus.FAILED},
            {"cleanup_status": CleanupStatus.KEPT},
            {"cleanup_status": CleanupStatus.UNKNOWN},
            {"secondary_errors": ["synthetic export failure"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertEqual(calculate_suite_outcome([replace(self.task, **mutation), pending]),
                                 (ExecutionStatus.FAILED, 1))
        self.assertEqual(calculate_suite_outcome([self.task, pending], has_export_or_secondary_errors=True),
                         (ExecutionStatus.FAILED, 1))

    def test_pending_tasks_may_lack_evidence_without_becoming_execution_failures(self):
        pending = replace(self.task, execution_status=ExecutionStatus.NOT_RUN)
        for evidence in (EvidenceStatus.COMPLETE, EvidenceStatus.NOT_COLLECTED):
            with self.subTest(evidence=evidence):
                self.assertEqual(calculate_suite_outcome([
                    self.task, replace(pending, evidence_status=evidence)
                ]), (ExecutionStatus.INTERRUPTED, 1))

    def test_cancellation_remains_an_interruption_even_with_failure_evidence(self):
        for code in (130, 143):
            with self.subTest(code=code):
                self.assertEqual(calculate_suite_outcome(
                    [replace(self.task, execution_status=ExecutionStatus.FAILED)],
                    is_cancelled=True, interrupted_signal=code,
                    has_corruption_or_index_errors=True,
                ), (ExecutionStatus.CANCELLED, code))

    def test_timeout_fallback_converts_minutes_before_multiplying(self):
        positive = get_task_by_id_or_alias("wasm-abi-boundary").to_dict()
        for minutes in (15, "15"):
            with self.subTest(minutes=minutes):
                document = dict(positive, timeout_minutes=minutes)
                del document["stage_timeout_seconds"]
                restored = TaskPlan.from_dict(document)
                self.assertEqual(restored.timeout_minutes, 15)
                self.assertEqual(restored.stage_timeout_seconds, 900)
        positive["stage_timeout_seconds"] = 123
        self.assertEqual(TaskPlan.from_dict(positive).stage_timeout_seconds, 123)


if __name__ == "__main__":
    unittest.main()
