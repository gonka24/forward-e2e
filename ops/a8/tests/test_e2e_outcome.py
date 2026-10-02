"""E2E run outcome evaluation, verdict recalculation, and integrity verification.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ops.a8.catalog import get_task_by_id_or_alias
from ops.a8.e2e.cli import cmd_recover, cmd_report, parse_e2e_args
from ops.a8.e2e.executor import ExecutionRequest, execute_plan, grade_run_package
from ops.a8.e2e.errors import ExportConflict
from ops.a8.e2e.outcome import HISTORICAL_REGRADE_FILENAME, RunOutcome, RunStatus
from ops.a8.e2e.compat import CompatibilityAdapter
from ops.a8.e2e.runner_image import RunnerImageIdentity
from ops.a8.e2e.runlock import (
    BUILD_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    BuildManifest,
    ExecutionManifest,
    PROVENANCE_HISTORICAL_PREPARED,
    content_sha256,
    write_run_lock,
)
from ops.a8.e2e.runpackage import RUN_RESULT_FILENAME, run_stage_dir
from ops.a8.evidence_model import EVIDENCE_MODEL_E2E
from ops.a8.models import CleanupStatus, ExecutionStatus
from ops.a8.tests.support.fakes import (
    CONTRACTS_SHA,
    GONKA_SHA,
    GONKA_TREE_SHA,
    MARKETPLACE_TREE_SHA,
    RUNNER_IMAGE_ID,
    FakeAcquirer,
    StubLayout,
    fake_process_runner,
    snapshot_fingerprint,
    write_suite_output,
)
from ops.a8.tests.support.packages import (
    make_baseline_lock,
    resync_artifact_index,
    setup_baseline_package,
)


class TestE2EOutcomeVerification(unittest.TestCase):
    """Verifies outcome grading, suite integrity violations, and verdict recalculations."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-outcome-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.pkg_dir = self.root / "package"
        self.out_dir = self.root / "out"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_historical_original_appearing_during_verdict_write_is_preserved(self):
        """A competing original verdict must never be overwritten. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.pkg_dir.mkdir()
        target = self.pkg_dir / RUN_RESULT_FILENAME
        original_exists = Path.exists
        inserted = False

        def original_appears(path):
            nonlocal inserted
            if path == target and not inserted:
                target.write_bytes(b"historical-original\n")
                inserted = True
                return False
            return original_exists(path)

        outcome = RunOutcome(provenance_model=PROVENANCE_HISTORICAL_PREPARED)
        with patch.object(Path, "exists", new=original_appears):
            with self.assertRaises(ExportConflict):
                outcome.write(target)
        self.assertEqual(target.read_bytes(), b"historical-original\n")
        self.assertIsNone(outcome.written_path)

    def test_historical_regrade_is_written_beside_an_existing_original(self):
        """Regrading an old package preserves its original result. All fixtures are synthetic. No network, Docker, or live chain calls."""
        self.pkg_dir.mkdir()
        target = self.pkg_dir / RUN_RESULT_FILENAME
        target.write_bytes(b"historical-original\n")
        outcome = RunOutcome(provenance_model=PROVENANCE_HISTORICAL_PREPARED)

        written = outcome.write(target)
        self.assertEqual(written, self.pkg_dir / HISTORICAL_REGRADE_FILENAME)
        self.assertEqual(target.read_bytes(), b"historical-original\n")
        self.assertEqual(json.loads(written.read_text(encoding="utf-8"))["status"], RunStatus.INCOMPLETE)

    def test_missing_execution_source_fingerprint_cannot_keep_a_pass(self):
        """Each locked source needs its post-execution fingerprint in both manifests. All fixtures are synthetic. No network, Docker, or live chain calls."""
        run_id = "e2e-outcome-missing-execution-fingerprint"
        setup_baseline_package(self.pkg_dir, scenarios=["wasm-abi-boundary"], run_id=run_id)
        self.assertEqual(grade_run_package(self.pkg_dir).status, RunStatus.PASSED)
        execution_path = self.pkg_dir / EXECUTION_MANIFEST_FILENAME
        execution = ExecutionManifest.from_dict(json.loads(execution_path.read_text(encoding="utf-8")))
        execution.source_immutability["after_execution"].pop("gonka")
        execution.write(execution_path)

        regraded = grade_run_package(self.pkg_dir)
        self.assertNotEqual(regraded.status, RunStatus.PASSED)
        self.assertIn("INCOMPLETE_SOURCE_IMMUTABILITY_MISSING",
                      {finding.code for finding in regraded.findings})

    def test_go_boundary_raw_results_cannot_be_relocated_to_unrelated_directory(self):
        run_id = "e2e-go-boundary-relocated-raw"
        setup_baseline_package(self.pkg_dir, scenarios=["go-boundary"], run_id=run_id)
        self.assertEqual(grade_run_package(self.pkg_dir).status, RunStatus.PASSED)

        suite_dir = self.pkg_dir / "suite" / run_id
        plan_path = suite_dir / "suite-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["tasks"][0]["expected_artifacts"] = []
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

        raw = next((suite_dir / "runs").glob("*/raw/go-test.json"))
        unrelated = raw.parent.parent / "unrelated" / raw.name
        unrelated.parent.mkdir()
        raw.rename(unrelated)
        index_path = suite_dir / "artifact-index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        old_rel = raw.relative_to(suite_dir).as_posix()
        new_rel = unrelated.relative_to(suite_dir).as_posix()
        for item in index["artifacts"]:
            if item["relative_path"] == old_rel:
                item["relative_path"] = new_rel
                break
        else:
            self.fail(f"Baseline package did not index {old_rel}")
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

        outcome = grade_run_package(self.pkg_dir)
        self.assertNotEqual(outcome.status, RunStatus.PASSED)
        self.assertTrue(
            any("raw/go-test.json" in str(finding.details) or "raw/go-test.json" in finding.message
                for finding in outcome.findings),
            outcome.findings,
        )

    def test_go_boundary_report_cannot_name_a_different_raw_event_hash(self):
        run_id = "e2e-go-boundary-foreign-raw-hash"
        setup_baseline_package(self.pkg_dir, scenarios=["go-boundary"], run_id=run_id)
        self.assertEqual(grade_run_package(self.pkg_dir).status, RunStatus.PASSED)

        suite_dir = self.pkg_dir / "suite" / run_id
        report_path = next((suite_dir / "runs").glob("*/report.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["test_output_sha256"] = "0" * 64
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        resync_artifact_index(suite_dir)

        outcome = grade_run_package(self.pkg_dir)
        self.assertNotEqual(outcome.status, RunStatus.PASSED)
        self.assertTrue(
            any("test_output_sha256" in finding.message or "test_output_sha256" in str(finding.details)
                for finding in outcome.findings),
            outcome.findings,
        )

    def test_boundary_package_missing_abi_report_fails_grader_and_cmd_report_with_nonzero_exit(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["wasm-abi-boundary"], run_id=run_id)
        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.status, RunStatus.PASSED)

        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*wasm-abi-boundary*"))
        abi_file = task_run_dir / "abi.json"
        self.assertTrue(abi_file.is_file(), f"Expected abi file {abi_file} to exist")
        abi_file.unlink()

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertTrue(
            any(f.code in ("SUITE_INTEGRITY_VIOLATION", "MISSING_ARTIFACT") for f in outcome.findings),
            f"Expected integrity or artifact violation in findings: {outcome.findings}",
        )

        _, args = parse_e2e_args(["report", "--run", str(self.pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

    def test_native_package_missing_junit_fails_grader_and_cmd_report(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["lock-exact-e"], run_id=run_id)
        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.status, RunStatus.PASSED)

        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*lock-exact-e*"))
        junit_file = task_run_dir / "junit" / "TEST-MarketplaceContractAcceptanceTests.xml"
        self.assertTrue(junit_file.is_file(), f"Expected junit file {junit_file} to exist")
        junit_file.unlink()

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertTrue(
            any(f.code in ("SUITE_INTEGRITY_VIOLATION", "MISSING_ARTIFACT") for f in outcome.findings),
            f"Expected integrity or artifact violation in findings: {outcome.findings}",
        )

        _, args = parse_e2e_args(["report", "--run", str(self.pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

    def test_boundary_package_missing_abi_artifact_fails_grader_and_cmd_report(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["wasm-abi-boundary"], run_id=run_id)
        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*wasm-abi-boundary*"))
        wasm_file = task_run_dir / "a8_query_boundary.wasm"
        self.assertTrue(wasm_file.is_file(), f"Expected wasm file {wasm_file} to exist")
        wasm_file.unlink()

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)

        _, args = parse_e2e_args(["report", "--run", str(self.pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

    def test_native_package_modified_receipt_fails_grader_and_cmd_report(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["lock-exact-e"], run_id=run_id)
        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.status, RunStatus.PASSED)

        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*lock-exact-e*"))
        result_file = task_run_dir / "result.json"
        res_data = json.loads(result_file.read_text(encoding="utf-8"))
        res_data["observed_passed_cases"] = []
        result_file.write_text(json.dumps(res_data, indent=2), encoding="utf-8")

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)

        _, args = parse_e2e_args(["report", "--run", str(self.pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

    def test_malformed_live_context_json_yields_document_unreadable_and_fails_grader(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["lock-exact-e"], run_id=run_id)
        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*lock-exact-e*"))
        ctx_file = task_run_dir / "live-context.json"
        self.assertTrue(ctx_file.is_file(), f"Expected live-context file {ctx_file} to exist")
        ctx_file.write_text("{this is not valid json content!!!\n", encoding="utf-8")
        resync_artifact_index(self.pkg_dir / "suite" / run_id)

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        unreadable = [f for f in outcome.findings if f.code == "DOCUMENT_UNREADABLE"]
        self.assertTrue(unreadable, outcome.findings)
        self.assertEqual(
            [f.details["expected_path"] for f in unreadable],
            [ctx_file.relative_to(self.pkg_dir / "suite" / run_id).as_posix()],
        )

    def test_suite_plan_native_to_boundary_mismatch_yields_selection_disagreement_and_fails_grader(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["lock-exact-e"], run_id=run_id)
        plan_file = self.pkg_dir / "suite" / run_id / "suite-plan.json"
        plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
        boundary_task = get_task_by_id_or_alias("wasm-abi-boundary")
        plan_data["tasks"] = [boundary_task.to_dict()]
        plan_data["requested_scenarios"] = ["wasm-abi-boundary"]
        plan_file.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertTrue(
            any(f.code == "SELECTION_DISAGREEMENT" for f in outcome.findings),
            f"Expected SELECTION_DISAGREEMENT in findings: {outcome.findings}",
        )

    def test_export_failure_during_execution_yields_nonzero_exit_and_durable_manifest_record(self):
        workspace = self.root / "workspace"
        output = self.root / "output"
        g_bytes = b"G"
        c_bytes = b"C"
        lock = make_baseline_lock(
            ["wasm-abi-boundary"],
            gonka_bundle_sha256=hashlib.sha256(g_bytes).hexdigest(),
            contracts_bundle_sha256=hashlib.sha256(c_bytes).hexdigest(),
        )
        lock_path = self.root / "test.lock.json"
        write_run_lock(lock, lock_path)

        bundle_dir = self.root / "bundles"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "gonka.bundle").write_bytes(g_bytes)
        (bundle_dir / "contracts.bundle").write_bytes(c_bytes)

        def make_test_adapter(role, lock, layout):
            return CompatibilityAdapter(
                adapter_id=f"{role}-test-adapter",
                role=role,
                description="synthetic adapter for offline tests",
                verified_commits=frozenset(),
                markers=(),
                build=None,
                runtime=(),
                harness=None,
            )

        def capture_snapshot(root: Path) -> dict:
            if Path(root).name == "gonka":
                return snapshot_fingerprint(GONKA_SHA, GONKA_TREE_SHA)
            return snapshot_fingerprint(CONTRACTS_SHA, MARKETPLACE_TREE_SHA)

        git = MagicMock(name="git-client")
        git.stdout.return_value = GONKA_SHA[:12]

        with patch("ops.a8.e2e.delivery.export_run_package", side_effect=OSError("Disk write failed")), \
             patch("ops.a8.e2e.executor.rebuild_adapter", side_effect=make_test_adapter), \
             patch("ops.a8.e2e.executor.RunnerLayout", return_value=StubLayout(self.root)), \
             patch("ops.a8.e2e.executor.resolve_runner_image", return_value=RunnerImageIdentity(locator="a8-runner:local", image_id=RUNNER_IMAGE_ID, repo_digest=None, locator_is_immutable=False, portability="local-image-only", resolved_by="launcher-injected")), \
             patch("ops.a8.e2e.executor.assert_runner_matches_lock", return_value=None), \
             patch("ops.a8.e2e.executor.SourceAcquirer", FakeAcquirer):
            req = ExecutionRequest(
                lock=lock,
                lock_path=lock_path,
                workspace_dir=workspace,
                output_dir=output,
                package_dir=self.root,
                run_id="e2e-20260101-000000-r12exportfail",
            )
            exit_code = execute_plan(
                req,
                git=git,
                build_runner=fake_process_runner,
                docker_runner=fake_process_runner,
                snapshot_capture=capture_snapshot,
                suite_runner=lambda **kwargs: (
                    write_suite_output(
                        output_dir=kwargs["output_dir"],
                        suite_id=kwargs["suite_id"],
                        profile=kwargs["profile"],
                        scenarios=kwargs["scenarios"],
                        e2e_context=kwargs["e2e_context"],
                    )
                    and 0
                ),
            )
            self.assertEqual(exit_code, 1)

            stage = run_stage_dir(workspace, req.run_id)
            self.assertTrue((stage / RUN_LOCK_FILENAME).is_file())
            self.assertTrue((stage / EXECUTION_MANIFEST_FILENAME).is_file())
            delivery = json.loads((stage / "delivery.json").read_text(encoding="utf-8"))
            self.assertEqual(delivery["status"], "FAILED")
            self.assertEqual(len(delivery["attempts"]), 1)
            attempt = delivery["attempts"][0]
            self.assertEqual(attempt["command"], "run")
            self.assertEqual(attempt["status"], "FAILED")
            self.assertEqual(attempt["error"], "Disk write failed")
            self.assertTrue(attempt["completed_at_utc"])

        _, rec_args = parse_e2e_args(["recover", "--run", req.run_id, "--workspace", str(workspace), "--output", str(self.root / "recovered_out")])
        recovery_messages: list[str] = []
        rec_code = cmd_recover(rec_args, emit=recovery_messages.append)
        self.assertEqual(rec_code, 0, recovery_messages)
        self.assertTrue((self.root / "recovered_out" / req.run_id / RUN_LOCK_FILENAME).is_file())

    def test_failure_of_one_task_in_mixed_selection_blocks_entire_run_verdict(self):
        run_id = "e2e-outcome-run"
        setup_baseline_package(
            self.pkg_dir,
            scenarios=["wasm-abi-boundary", "lock-exact-e"],
            run_id=run_id,
            execution_status=ExecutionStatus.PASSED,
        )
        task_run_dir = next((self.pkg_dir / "suite" / run_id / "runs").glob("*lock-exact-e*"))
        res_file = task_run_dir / "result.json"
        res_data = json.loads(res_file.read_text(encoding="utf-8"))
        res_data["execution_status"] = ExecutionStatus.FAILED.value
        res_data["exit_code"] = 1
        res_file.write_text(json.dumps(res_data, indent=2), encoding="utf-8")

        resync_artifact_index(self.pkg_dir / "suite" / run_id)

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.suite_status, ExecutionStatus.FAILED.value)
        codes = [f.code for f in outcome.findings]
        self.assertIn("SUITE_RESULT_DISAGREEMENT", codes)
        self.assertNotIn("SUITE_INTEGRITY_VIOLATION", codes)

    def test_stale_overall_passed_with_failed_task_cleanup_is_graded_failed_and_nonzero(self):
        """Task cleanup FAILED must cause recalculated overall FAILED even if stored overall_status is PASSED."""
        run_id = "e2e-outcome-run"
        setup_baseline_package(self.pkg_dir, scenarios=["go-boundary", "ct-network-unconfirmed"], run_id=run_id)
        suite_dir = self.pkg_dir / "suite" / run_id
        task_res_file = next((suite_dir / "runs").glob("*go-boundary*")) / "result.json"

        tr_data = json.loads(task_res_file.read_text(encoding="utf-8"))
        tr_data["cleanup_status"] = CleanupStatus.FAILED.value
        task_res_file.write_text(json.dumps(tr_data, indent=2) + "\n", encoding="utf-8")
        resync_artifact_index(suite_dir)

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.suite_status, ExecutionStatus.FAILED.value)
        codes = [f.code for f in outcome.findings]
        self.assertIn("SUITE_RESULT_DISAGREEMENT", codes)

        messages: list[str] = []
        argv = ["report", "--run", str(self.pkg_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

        verdict = json.loads((self.pkg_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(verdict["status"], RunStatus.FAILED)
        self.assertEqual(verdict["suite_status"], ExecutionStatus.FAILED.value)

    def test_stale_overall_passed_with_task_defect_is_recalculated_as_non_passing(self):
        """Task defects must cause recalculated overall failure/interruption and non-zero exit."""
        cases = (
            (
                "failed_task_execution",
                "ct-network-unconfirmed",
                {
                    "execution_status": ExecutionStatus.FAILED.value,
                    "exit_code": 1,
                    "primary_failure": "Cargo test assertion failed",
                },
                ExecutionStatus.FAILED.value,
                "SUITE_RESULT_DISAGREEMENT",
            ),
            (
                "not_run_task",
                "ct-network-unconfirmed",
                {
                    "execution_status": ExecutionStatus.NOT_RUN.value,
                },
                ExecutionStatus.INTERRUPTED.value,
                None,
            ),
            (
                "secondary_errors",
                "go-boundary",
                {
                    "secondary_errors": ["Container log extraction failed"],
                },
                ExecutionStatus.FAILED.value,
                None,
            ),
        )

        for name, scenario, mutations, expected_suite_status, expected_finding in cases:
            with self.subTest(defect=name):
                pkg_dir = self.root / f"package-{name}"
                run_id = "e2e-outcome-run"
                setup_baseline_package(
                    pkg_dir,
                    scenarios=["go-boundary", "ct-network-unconfirmed"],
                    run_id=run_id,
                )
                suite_dir = pkg_dir / "suite" / run_id
                task_res_file = next((suite_dir / "runs").glob(f"*{scenario}*")) / "result.json"

                tr_data = json.loads(task_res_file.read_text(encoding="utf-8"))
                tr_data.update(mutations)
                task_res_file.write_text(json.dumps(tr_data, indent=2) + "\n", encoding="utf-8")
                resync_artifact_index(suite_dir)

                outcome = grade_run_package(pkg_dir)
                self.assertEqual(outcome.exit_code, 1)
                self.assertEqual(outcome.status, RunStatus.FAILED)
                self.assertEqual(outcome.suite_status, expected_suite_status)
                if expected_finding is not None:
                    self.assertIn(expected_finding, [f.code for f in outcome.findings])

    def test_historical_evidence_model_package_fails_grader_with_historical_evidence_model_not_accepted(self):
        run_id = "e2e-outcome-historical"
        setup_baseline_package(self.pkg_dir, scenarios=["lock-exact-e"], run_id=run_id)
        suite_dir = self.pkg_dir / "suite" / run_id
        e2e_ctx_file = suite_dir / "e2e-context.json"
        ctx_data = json.loads(e2e_ctx_file.read_text(encoding="utf-8"))
        ctx_data["evidence_model"] = EVIDENCE_MODEL_E2E
        e2e_ctx_file.write_text(json.dumps(ctx_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        outcome = grade_run_package(self.pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertIn("HISTORICAL_EVIDENCE_MODEL_NOT_ACCEPTED", [f.code for f in outcome.findings])

    def test_source_immutability_verdict_violation_or_incompleteness_in_build_manifest_fails_grader(self):
        for verdict_val, expected_code in (
            ("VIOLATED", "SOURCE_IMMUTABILITY_VIOLATED"),
            ("INCOMPLETE", "SOURCE_IMMUTABILITY_INCOMPLETE"),
        ):
            with self.subTest(verdict=verdict_val):
                pkg_dir = self.root / f"package-immut-{verdict_val.lower()}"
                run_id = "e2e-outcome-immut"
                setup_baseline_package(pkg_dir, scenarios=["wasm-abi-boundary"], run_id=run_id)
                build_path = pkg_dir / BUILD_MANIFEST_FILENAME
                manifest = BuildManifest.from_dict(json.loads(build_path.read_text(encoding="utf-8")))
                manifest.source_immutability["verdict"] = verdict_val
                manifest.write(build_path)
                exec_path = pkg_dir / EXECUTION_MANIFEST_FILENAME
                exec_manifest = ExecutionManifest.from_dict(json.loads(exec_path.read_text(encoding="utf-8")))
                exec_manifest.build_manifest_sha256 = content_sha256(manifest.to_dict())
                exec_manifest.write(exec_path)

                outcome = grade_run_package(pkg_dir)
                self.assertEqual(outcome.exit_code, 1)
                self.assertEqual(outcome.status, RunStatus.FAILED)
                self.assertIn(expected_code, [f.code for f in outcome.findings])


if __name__ == "__main__":
    unittest.main()
