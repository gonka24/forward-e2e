"""Tests proving package resolution, parent/child plan resolution, and legacy package compatibility.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import builtins
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.execution.cli import cmd_report, parse_e2e_args
from forward_e2e.execution.outcome import RUN_RESULT_FILENAME, evaluate_run
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    DeliveryStatus,
    write_run_lock,
)
from forward_e2e.execution.runpackage import LoadedRunPackage, run_stage_dir
from tests.unit.runner.support.fakes import write_suite_output
from tests.unit.runner.support.packages import (
    make_baseline_lock,
    setup_baseline_package,
)


class TestE2EPackageResolution(unittest.TestCase):
    """Proves resolution of package structures, boundaries, and delivery manifest requirements."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.output = self.root / "output"
        self.output.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_package_without_delivery_reference_passes_when_standard_delivery_file_exists(self):
        """The optional reference is not required to discover the standard delivery file."""
        run_id = "e2e-no-delivery-relpath"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=run_id,
            delivery_manifest_relpath=None,
        )
        self.assertTrue((stage_dir / DELIVERY_MANIFEST_FILENAME).is_file())
        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "PASSED")

    def test_package_without_delivery_file_is_incomplete_with_delivery_manifest_missing(self):
        """An intact reference cannot compensate for the absent delivery document."""
        run_id = "e2e-missing-delivery-file"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=run_id,
            include_delivery_file=False,
        )
        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "INCOMPLETE")
        self.assertIn("DELIVERY_MANIFEST_MISSING", [f["code"] for f in res["findings"]])

    def test_boundary_package_missing_suite_plan_fails_with_suite_plan_missing_finding(self):
        """A boundary run package missing suite-plan.json fails grading."""
        run_id = "e2e-res-boundary-missing-plan"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        plan_file = stage_dir / "suite" / run_id / "suite-plan.json"
        plan_file.unlink()

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("SUITE_PLAN_MISSING", [f["code"] for f in res["findings"]])

    def test_boundary_package_unsupported_suite_plan_schema_fails_with_blocking_finding(self):
        """A boundary run package with unsupported suite-plan schema fails grading."""
        run_id = "e2e-res-boundary-schema"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        plan_file = stage_dir / "suite" / run_id / "suite-plan.json"
        data = json.loads(plan_file.read_text(encoding="utf-8"))
        data["schema_version"] = "unsupported-99.9"
        plan_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("SUITE_PLAN_UNSUPPORTED", [f["code"] for f in res["findings"]])

    def test_successful_boundary_package_passes_without_deployment_evidence(self):
        """Boundary-only scenario (go-boundary) passes without deployment evidence."""
        run_id = "e2e-res-boundary-pass"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "PASSED")

    def test_e2e_package_missing_lock_is_graded_as_incomplete_lock_missing_across_resolution_paths(self):
        """Package missing run.lock.json is graded INCOMPLETE_LOCK_MISSING."""
        run_id = "e2e-res-missing-lock"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        (stage_dir / RUN_LOCK_FILENAME).unlink()

        targets = (
            ["--run", str(stage_dir)],
            ["--run", run_id, "--workspace", str(self.workspace)],
            ["--run", str(stage_dir / "suite" / run_id)],
        )
        for target in targets:
            with self.subTest(target=target):
                verdict = stage_dir / RUN_RESULT_FILENAME
                # A previous report must not become the next case's recognition marker.
                if verdict.exists():
                    verdict.unlink()
                _, args = parse_e2e_args(["report", *target])
                self.assertEqual(cmd_report(args, emit=lambda _: None), 1)
                res = json.loads(verdict.read_text(encoding="utf-8"))
                self.assertEqual(res["status"], "INCOMPLETE")
                self.assertEqual(res["run_id"], run_id)
                self.assertIn("INCOMPLETE_LOCK_MISSING", [f["code"] for f in res["findings"]])

    def test_e2e_package_missing_lock_with_unsafe_suite_export_relpath_does_not_open_or_enumerate_external_files(self):
        """Traversal is rejected before opening external files or enumerating external directories."""
        run_id = "e2e-res-unsafe-relpath"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        (stage_dir / RUN_LOCK_FILENAME).unlink()
        exec_file = stage_dir / EXECUTION_MANIFEST_FILENAME
        data = json.loads(exec_file.read_text(encoding="utf-8"))
        data["suite_export_relpath"] = "../../outside_attack"
        exec_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        outside = (stage_dir / data["suite_export_relpath"]).resolve()
        self.assertEqual(outside, self.workspace / "outside_attack")
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        external_accesses = []
        descriptor_paths = {}
        original_os_open = os.open

        def guard(operation):
            def checked(path, *args, **kwargs):
                if isinstance(path, int):
                    absolute = descriptor_paths.get(path)
                else:
                    candidate = Path(os.fsdecode(path))
                    directory_fd = kwargs.get("dir_fd")
                    if not candidate.is_absolute() and directory_fd is not None:
                        # Atomic publication opens directories first, then uses relative names.
                        candidate = descriptor_paths[directory_fd] / candidate
                    absolute = Path(os.path.abspath(candidate))
                if absolute is not None and (absolute == outside or outside in absolute.parents):
                    external_accesses.append(str(absolute))
                    raise AssertionError(f"Unexpected external filesystem access: {absolute}")
                result = operation(path, *args, **kwargs)
                if operation is original_os_open:
                    descriptor_paths[result] = absolute
                return result
            return checked

        # Cover file opens and both directory-listing APIs, including calls via pathlib.
        # Checking final contents alone would miss reads of the external directory.
        with ExitStack() as stack:
            for module, name in (
                (builtins, "open"), (io, "open"), (os, "open"),
                (os, "scandir"), (os, "listdir"),
            ):
                stack.enter_context(patch.object(module, name, guard(getattr(module, name))))
            _, args = parse_e2e_args(["report", "--run", str(stage_dir)])
            self.assertEqual(cmd_report(args, emit=lambda _: None), 1)

        # cmd_report catches exceptions and may return without writing a verdict.
        # Report forbidden access before a missing verdict can obscure its cause.
        self.assertEqual(external_accesses, [])
        res = json.loads((stage_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("SUITE_PATH_UNSAFE", [f["code"] for f in res["findings"]])
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_package_with_only_build_manifest_or_e2e_context_is_not_treated_as_legacy_success(self):
        """Each surviving provenance marker independently identifies an incomplete E2E package."""
        run_id = "e2e-res-partial-provenance"
        baseline = self.root / "baseline"
        setup_baseline_package(baseline, scenarios=["go-query-error-classification"], run_id=run_id)

        # Preserve producer bytes for each partial package; do not invent marker documents.
        cases = (
            (Path(BUILD_MANIFEST_FILENAME), "INCOMPLETE"),
            # The context identifies an E2E package, but its suite has no mandatory plan.
            (Path("suite") / run_id / "e2e-context.json", "FAILED"),
        )
        for marker, expected_status in cases:
            with self.subTest(marker=marker):
                package = self.output / marker.stem / run_id
                destination = package / marker
                destination.parent.mkdir(parents=True)
                destination.write_bytes((baseline / marker).read_bytes())
                self.assertEqual([p.relative_to(package) for p in package.rglob("*") if p.is_file()], [marker])

                _, args = parse_e2e_args(["report", "--run", str(package)])
                self.assertEqual(cmd_report(args, emit=lambda _: None), 1)
                res = json.loads((package / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(res["status"], expected_status)
                self.assertIn("INCOMPLETE_LOCK_MISSING", [f["code"] for f in res["findings"]])

    def test_bare_legacy_suite_is_rejected_as_an_e2e_run_package(self):
        """A bare legacy suite is rejected with an explanatory message and exit code 1."""
        suite_id = "legacy-suite-001"
        bare_suite = self.output / suite_id
        write_suite_output(
            output_dir=self.output,
            suite_id=suite_id,
            profile=None,
            scenarios=["go-query-error-classification"],
            e2e_context=None,
        )

        messages: list[str] = []
        argv = ["report", "--run", str(bare_suite)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)
        self.assertTrue(any("a bare suite directory, not an E2E run package" in m for m in messages))
        self.assertTrue(any("Bare suite directories without an E2E run package envelope cannot produce a passing E2E verdict" in m for m in messages))

    def test_parent_plan_with_child_completed_run_resolves_to_child_and_leaves_parent_untouched_via_run_path(self):
        """Passing --run <out>/<run-id> grades the child run and leaves parent plan untouched."""
        out_dir = self.output / "parent_run_path"
        out_dir.mkdir(parents=True, exist_ok=True)
        parent_lock = make_baseline_lock(["go-query-error-classification"], plan_id="parent-plan-001")
        write_run_lock(parent_lock, out_dir / RUN_LOCK_FILENAME)
        parent_lock_bytes = (out_dir / RUN_LOCK_FILENAME).read_bytes()

        run_id = "e2e-child-001"
        child_dir = out_dir / run_id
        setup_baseline_package(child_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        argv = ["report", "--run", str(child_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=lambda _: None)

        self.assertEqual(code, 0)
        self.assertTrue((child_dir / RUN_RESULT_FILENAME).is_file())
        res = json.loads((child_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(res["status"], "PASSED")
        self.assertEqual(res["run_id"], run_id)
        self.assertFalse((out_dir / RUN_RESULT_FILENAME).exists())
        self.assertEqual((out_dir / RUN_LOCK_FILENAME).read_bytes(), parent_lock_bytes)

    def test_parent_plan_with_child_completed_run_resolves_to_child_via_run_id_and_output(self):
        """Passing --run <run-id> --output <out> grades the child run and leaves parent plan untouched."""
        out_dir = self.output / "parent_run_id"
        out_dir.mkdir(parents=True, exist_ok=True)
        parent_lock = make_baseline_lock(["go-query-error-classification"], plan_id="parent-plan-002")
        write_run_lock(parent_lock, out_dir / RUN_LOCK_FILENAME)
        parent_lock_bytes = (out_dir / RUN_LOCK_FILENAME).read_bytes()

        run_id = "e2e-child-002"
        child_dir = out_dir / run_id
        setup_baseline_package(child_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        argv = ["report", "--run", run_id, "--output", str(out_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=lambda _: None)

        self.assertEqual(code, 0)
        self.assertTrue((child_dir / RUN_RESULT_FILENAME).is_file())
        res = json.loads((child_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(res["status"], "PASSED")
        self.assertFalse((out_dir / RUN_RESULT_FILENAME).exists())
        self.assertEqual((out_dir / RUN_LOCK_FILENAME).read_bytes(), parent_lock_bytes)

    def test_parent_plan_with_child_completed_run_resolves_to_child_via_nested_suite(self):
        """Passing nested suite <out>/<run-id>/suite/<run-id> resolves to closest child run, not parent plan."""
        out_dir = self.output / "parent_nested"
        out_dir.mkdir(parents=True, exist_ok=True)
        parent_lock = make_baseline_lock(["go-query-error-classification"], plan_id="parent-plan-003")
        write_run_lock(parent_lock, out_dir / RUN_LOCK_FILENAME)
        parent_lock_bytes = (out_dir / RUN_LOCK_FILENAME).read_bytes()

        run_id = "e2e-child-003"
        child_dir = out_dir / run_id
        setup_baseline_package(child_dir, scenarios=["go-query-error-classification"], run_id=run_id)

        nested_suite = child_dir / "suite" / run_id
        messages: list[str] = []
        argv = ["report", "--run", str(nested_suite)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)

        self.assertEqual(code, 0)
        self.assertTrue(any(f"is part of the run package at {child_dir}" in m for m in messages))
        self.assertTrue((child_dir / RUN_RESULT_FILENAME).is_file())
        res = json.loads((child_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(res["status"], "PASSED")
        self.assertFalse((out_dir / RUN_RESULT_FILENAME).exists())
        self.assertEqual((out_dir / RUN_LOCK_FILENAME).read_bytes(), parent_lock_bytes)

    def test_parent_plan_with_child_missing_lock_grades_child_as_incomplete_not_shadowed_by_parent_lock(self):
        """Parent lock does not mask a missing lock in the child run."""
        plan_dir = self.workspace / "plan-002"
        plan_dir.mkdir(parents=True, exist_ok=True)
        lock = make_baseline_lock(["go-query-error-classification"], plan_id="plan-002")
        write_run_lock(lock, plan_dir / RUN_LOCK_FILENAME)

        child_dir = plan_dir / "runs" / "run-002"
        setup_baseline_package(child_dir, scenarios=["go-query-error-classification"], run_id="run-002")
        (child_dir / RUN_LOCK_FILENAME).unlink()

        parent_lock_bytes = (plan_dir / RUN_LOCK_FILENAME).read_bytes()
        for target in (child_dir, child_dir / "suite" / "run-002"):
            with self.subTest(target=target):
                verdict = child_dir / RUN_RESULT_FILENAME
                if verdict.exists():
                    verdict.unlink()
                _, args = parse_e2e_args(["report", "--run", str(target)])
                self.assertEqual(cmd_report(args, emit=lambda _: None), 1)
                res = json.loads(verdict.read_text(encoding="utf-8"))
                self.assertEqual(res["status"], "INCOMPLETE")
                self.assertEqual(res["run_id"], "run-002")
                self.assertIn("INCOMPLETE_LOCK_MISSING", [f["code"] for f in res["findings"]])
                self.assertFalse((plan_dir / RUN_RESULT_FILENAME).exists())
                self.assertEqual((plan_dir / RUN_LOCK_FILENAME).read_bytes(), parent_lock_bytes)

    def test_reporting_directly_on_unexecuted_plan_package_identifies_it_as_incomplete_plan_not_legacy_suite(self):
        """Reporting directly on an unexecuted plan package produces INCOMPLETE_EXECUTION_MANIFEST_MISSING."""
        plan_dir = self.workspace / "plan-unexecuted"
        plan_dir.mkdir(parents=True, exist_ok=True)
        lock = make_baseline_lock(["go-query-error-classification"], plan_id="plan-unexecuted")
        write_run_lock(lock, plan_dir / RUN_LOCK_FILENAME)

        _, args = parse_e2e_args(["report", "--run", str(plan_dir)])
        self.assertEqual(cmd_report(args, emit=lambda _: None), 1)
        res = json.loads((plan_dir / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(res["status"], "INCOMPLETE")
        self.assertIn("INCOMPLETE_EXECUTION_MANIFEST_MISSING", [f["code"] for f in res["findings"]])

    def test_export_crash_window_in_progress_state_produces_export_not_completed(self):
        """A package with IN_PROGRESS delivery status produces EXPORT_NOT_COMPLETED."""
        run_id = "e2e-res-in-progress"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-query-error-classification"],
            run_id=run_id,
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("EXPORT_NOT_COMPLETED", [f["code"] for f in res["findings"]])
