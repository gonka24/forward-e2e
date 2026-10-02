"""Tests proving delivery manifest verification, atomic delivery, and lifecycle transitions.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from ops.a8.e2e import delivery as delivery_module
from ops.a8.e2e.cli import cmd_recover, cmd_report, parse_e2e_args
from ops.a8.e2e.delivery import CorruptDeliveryLedgerError, DeliveryBusyError, DeliveryService
from ops.a8.e2e.errors import ExportConflict, PathSafetyError
from ops.a8.e2e.executor import export_and_record_delivery
from ops.a8.e2e.outcome import evaluate_run
from ops.a8.e2e.runlock import (
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    write_delivery_manifest,
)
from ops.a8.e2e.runpackage import LoadedRunPackage, export_run_package, run_stage_dir
from ops.a8.tests.support.packages import (
    setup_baseline_package,
)


class TestE2EDelivery(unittest.TestCase):
    """Proves delivery manifest verification, transitions, and write safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.output = self.root / "output"
        self.output.mkdir(parents=True, exist_ok=True)
        self.outside = self.root / "outside"
        self.outside.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delivery_lock_rejects_parent_replaced_after_path_check(self):
        stage = self.workspace / "lock-stage"
        stage.mkdir()
        external_stage = self.outside / stage.name
        external_stage.mkdir()
        held_workspace = self.root / "held-workspace"
        real_check = delivery_module.assert_no_symlinks_in_path
        swapped = False

        def swap_after_check(path, *, what):
            nonlocal swapped
            real_check(path, what=what)
            if path == stage and what == "Delivery staging directory" and not swapped:
                self.workspace.rename(held_workspace)
                self.workspace.symlink_to(self.outside, target_is_directory=True)
                swapped = True

        with patch.object(delivery_module, "assert_no_symlinks_in_path", side_effect=swap_after_check):
            with self.assertRaises(PathSafetyError):
                with delivery_module._delivery_lock(stage):
                    pass
        self.assertTrue(swapped)

    def test_identity_read_rejects_a_destination_link_inserted_after_check(self):
        """A late link cannot make an external file satisfy destination identity. All fixtures are synthetic. No network, Docker, or live chain calls."""
        stage, dest = self.workspace / "identity-stage", self.output / "identity-dest"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source")
        dest.mkdir()
        identity_path = dest / "run.lock.json"
        external = self.outside / "matching-lock.json"
        external.write_bytes((stage / identity_path.name).read_bytes())
        identity_path.write_bytes(external.read_bytes())
        real_check = delivery_module.assert_no_symlinks_in_path
        inserted = False

        def swap_after_check(path, *, what):
            nonlocal inserted
            real_check(path, what=what)
            if path == identity_path and what == "Delivery destination identity" and not inserted:
                identity_path.unlink()
                identity_path.symlink_to(external)
                inserted = True

        with patch.object(delivery_module, "assert_no_symlinks_in_path", side_effect=swap_after_check):
            with self.assertRaises(PathSafetyError):
                delivery_module._assert_destination_identity(stage, dest, "source")
        self.assertTrue(inserted)
        self.assertEqual(external.read_bytes(), (stage / identity_path.name).read_bytes())

    def test_recovery_ledger_read_rejects_a_link_inserted_after_leaf_check(self):
        """A late ledger link cannot supply recovery history from outside staging. All fixtures are synthetic. No network, Docker, or live chain calls."""
        stage, dest = self.workspace / "ledger-stage", self.output / "ledger-dest"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source")
        ledger = stage / DELIVERY_MANIFEST_FILENAME
        external = self.outside / "matching-ledger.json"
        external.write_bytes(ledger.read_bytes())
        original_is_file = Path.is_file
        inserted = False

        def swap_on_is_file(path):
            nonlocal inserted
            if path == ledger and not inserted:
                ledger.unlink()
                ledger.symlink_to(external)
                inserted = True
                return True
            return original_is_file(path)

        with patch.object(Path, "is_file", new=swap_on_is_file):
            with self.assertRaises(CorruptDeliveryLedgerError):
                DeliveryService.deliver_recovery(stage, dest, suite_id="source")
        self.assertTrue(inserted)
        self.assertEqual(external.read_bytes(), (stage / DELIVERY_MANIFEST_FILENAME).read_bytes())

    def test_destination_parent_swap_cannot_create_outside_directory(self):
        """A swapped output parent must not receive a new run directory. All fixtures are synthetic. No network, Docker, or live chain calls."""
        stage = self.workspace / "directory-stage"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source")
        swappable = self.root / "swappable-output"
        swappable.mkdir()
        run_dir = swappable / "source"
        delivery = DeliveryManifest.from_dict(
            json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        )
        attempt = DeliveryAttempt(
            attempt_number=2, command="recover", started_at_utc="2026-01-01T00:00:00+00:00",
            destination=str(run_dir), status=DeliveryStatus.IN_PROGRESS,
        )
        delivery.attempts.append(attempt)
        real_check = delivery_module.assert_no_symlinks_in_path
        swapped = False

        def swap_after_check(path, *, what):
            nonlocal swapped
            real_check(path, what=what)
            if path == run_dir and what == "Recovery run directory" and not swapped:
                swappable.rename(self.root / "held-output")
                swappable.symlink_to(self.outside, target_is_directory=True)
                swapped = True

        with patch.object(delivery_module, "assert_no_symlinks_in_path", side_effect=swap_after_check):
            result = DeliveryService._execute_attempt(
                stage, run_dir, delivery, attempt, is_recovery=True,
            )
        self.assertTrue(swapped)
        self.assertFalse(result.success)
        self.assertFalse((self.outside / "source").exists())

    def test_missing_delivery_manifest_on_new_run_produces_incomplete_and_exit_1(self):
        """When an execution manifest declares delivery.json but it is missing, grading fails."""
        run_id = "e2e-delivery-missing"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            include_delivery_file=False,
        )

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "INCOMPLETE")
        self.assertIn("DELIVERY_MANIFEST_MISSING", [f["code"] for f in res["findings"]])

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

    def test_unknown_status_in_delivery_manifest_produces_invalid_finding_and_exit_1(self):
        """An unsupported status in delivery.json yields DELIVERY_MANIFEST_INVALID."""
        run_id = "e2e-delivery-invalid-status"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        delivery_file = stage_dir / DELIVERY_MANIFEST_FILENAME
        data = json.loads(delivery_file.read_text(encoding="utf-8"))
        data["status"] = "FLYING_IN_SPACE"
        delivery_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("DELIVERY_MANIFEST_INVALID", [f["code"] for f in res["findings"]])

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

    def test_mismatched_run_id_in_delivery_manifest_produces_identity_mismatch_and_exit_1(self):
        """A delivery manifest with a mismatched run_id yields DELIVERY_MANIFEST_IDENTITY_MISMATCH."""
        run_id = "e2e-delivery-mismatched-id"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        delivery_file = stage_dir / DELIVERY_MANIFEST_FILENAME
        data = json.loads(delivery_file.read_text(encoding="utf-8"))
        data["run_id"] = "e2e-different-run-id"
        delivery_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("DELIVERY_MANIFEST_IDENTITY_MISMATCH", [f["code"] for f in res["findings"]])

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

    def test_corrupted_attempts_structure_produces_invalid_finding_and_exit_1(self):
        """When attempts in delivery.json is not a valid list, DELIVERY_MANIFEST_INVALID is produced."""
        run_id = "e2e-delivery-bad-attempts"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        delivery_file = stage_dir / DELIVERY_MANIFEST_FILENAME
        data = json.loads(delivery_file.read_text(encoding="utf-8"))
        data["attempts"] = "not-a-list"
        delivery_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("DELIVERY_MANIFEST_INVALID", [f["code"] for f in res["findings"]])

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

    def test_completed_delivery_status_without_completed_attempt_produces_inconsistent_and_exit_1(self):
        """A top-level status of COMPLETED without any completed attempt produces DELIVERY_MANIFEST_INCONSISTENT."""
        run_id = "e2e-delivery-inconsistent"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        delivery_file = stage_dir / DELIVERY_MANIFEST_FILENAME
        data = json.loads(delivery_file.read_text(encoding="utf-8"))
        data["attempts"][0]["status"] = DeliveryStatus.FAILED
        delivery_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        loaded = LoadedRunPackage.load(stage_dir)
        res = evaluate_run(loaded).to_dict()
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("DELIVERY_MANIFEST_INCONSISTENT", [f["code"] for f in res["findings"]])

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 1)

    def test_happy_path_completed_run_and_recover_remains_passed(self):
        """A complete, valid package with matching delivery ledger grades as PASSED (exit 0)."""
        run_id = "e2e-delivery-happy-path"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.COMPLETED,
        )

        messages: list[str] = []
        argv = ["report", "--run", str(stage_dir)]
        _, args = parse_e2e_args(argv)
        code = cmd_report(args, emit=messages.append)
        self.assertEqual(code, 0)

        recover_out = self.root / "recovered-happy"
        argv_rec = [
            "recover",
            "--run",
            run_id,
            "--output",
            str(recover_out),
            "--workspace",
            str(self.workspace),
        ]
        _, args_rec = parse_e2e_args(argv_rec)
        rec_code = cmd_recover(args_rec, emit=lambda m: None)
        self.assertEqual(rec_code, 0)

        dest_pkg = recover_out / run_id
        argv_dest = ["report", "--run", str(dest_pkg)]
        _, args_dest = parse_e2e_args(argv_dest)
        dest_code = cmd_report(args_dest, emit=lambda m: None)
        self.assertEqual(dest_code, 0)

    def test_production_writer_rejects_symlink_leaf_and_protects_external_file(self):
        """write_delivery_manifest directly refuses symlinks and does not touch the link target."""
        sentinel_file = self.outside / "protected.txt"
        sentinel_file.write_text("INITIAL_UNTOUCHED_CONTENT", encoding="utf-8")

        symlink_path = self.output / "symlink_delivery.json"
        symlink_path.symlink_to(sentinel_file)

        delivery = DeliveryManifest(
            run_id="run-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[
                DeliveryAttempt(
                    attempt_number=1,
                    command="run",
                    started_at_utc="2026-01-01T00:00:00Z",
                    destination=str(symlink_path),
                    status=DeliveryStatus.COMPLETED,
                )
            ],
        )

        with self.assertRaises(PathSafetyError) as ctx:
            write_delivery_manifest(delivery, symlink_path, package_root=self.output)

        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertEqual(sentinel_file.read_text(encoding="utf-8"), "INITIAL_UNTOUCHED_CONTENT")

    def test_destination_final_delivery_write_failure_preserves_immutability_and_allows_recovery(self):
        """Simulate failure occurring only during the final write of delivery.json to destination."""
        run_id = "e2e-delivery-final-failure"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )

        exec_manifest_stage = stage_dir / EXECUTION_MANIFEST_FILENAME
        manifest_bytes_before = exec_manifest_stage.read_bytes()

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(
            json.loads(delivery_stage_path.read_text(encoding="utf-8"))
        )
        dest_delivery_path = run_dir / DELIVERY_MANIFEST_FILENAME
        export_error = OSError("Permission denied writing final delivery.json to destination")
        real_write = DeliveryManifest.write
        fault_called = [False]

        def faulty_write(manifest_self, target, **kwargs):
            if (
                Path(target) == dest_delivery_path
                and manifest_self.status == DeliveryStatus.COMPLETED
                and manifest_self.attempts
                and manifest_self.attempts[-1].status == DeliveryStatus.COMPLETED
            ):
                fault_called[0] = True
                raise export_error
            return real_write(manifest_self, target, **kwargs)

        with patch.object(DeliveryManifest, "write", faulty_write):
            graded_root, failure, ledger_failure = export_and_record_delivery(stage_dir, run_dir, delivery)
        self.assertTrue(fault_called[0], "Final destination write fault was not called")
        self.assertIsNotNone(failure)
        self.assertEqual(str(failure), str(export_error))
        self.assertIsNone(ledger_failure)
        self.assertEqual(graded_root, stage_dir)

        self.assertEqual(exec_manifest_stage.read_bytes(), manifest_bytes_before)
        failed_history = None
        for ledger_path in (delivery_stage_path, dest_delivery_path):
            persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], DeliveryStatus.FAILED)
            attempt = persisted["attempts"][-1]
            self.assertEqual(attempt["status"], DeliveryStatus.FAILED)
            self.assertTrue(attempt["completed_at_utc"])
            self.assertEqual(attempt["error"], str(export_error))
            if failed_history is None:
                failed_history = persisted["attempts"]
            else:
                self.assertEqual(persisted["attempts"], failed_history)

        messages: list[str] = []
        argv = [
            "recover",
            "--run",
            run_id,
            "--output",
            str(self.output),
            "--workspace",
            str(self.workspace),
        ]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)
        self.assertEqual(code, 0)

        dest_data = json.loads(dest_delivery_path.read_text(encoding="utf-8"))
        self.assertEqual(dest_data["attempts"][:-1], failed_history)
        stage_after = json.loads(delivery_stage_path.read_text(encoding="utf-8"))
        self.assertEqual(stage_after["attempts"], dest_data["attempts"])
        self.assertEqual(dest_data["status"], DeliveryStatus.COMPLETED)
        self.assertEqual(dest_data["attempts"][-1]["status"], DeliveryStatus.COMPLETED)
        self.assertIsNotNone(dest_data["attempts"][-1]["completed_at_utc"])
        self.assertIsNone(dest_data["attempts"][-1]["error"])

    def test_stage_finalization_failure_cannot_leave_a_passing_destination(self):
        """A failed staging ledger must leave destination IN_PROGRESS. All fixtures are synthetic. No network, Docker, or live chain calls."""
        run_id = "e2e-stage-ledger-success-failure"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(
            json.loads(delivery_stage_path.read_text(encoding="utf-8"))
        )
        stage_error = OSError("Staging disk readonly on success finalization")
        real_write = DeliveryManifest.write
        fault_called = [False]

        def faulty_write(manifest_self, target, **kwargs):
            if (
                Path(target) == delivery_stage_path
                and manifest_self.status == DeliveryStatus.COMPLETED
                and manifest_self.attempts
                and manifest_self.attempts[-1].status == DeliveryStatus.COMPLETED
            ):
                fault_called[0] = True
                raise stage_error
            return real_write(manifest_self, target, **kwargs)

        with patch.object(DeliveryManifest, "write", faulty_write):
            graded_root, failure, ledger_failure = export_and_record_delivery(stage_dir, run_dir, delivery)

        self.assertTrue(fault_called[0], "Stage ledger write fault was not called")
        self.assertIsNone(failure)
        self.assertEqual(ledger_failure, stage_error)
        self.assertEqual(graded_root, run_dir)

        # Evidence bytes were copied, but the two-ledger transaction did not finish.
        # A later offline report must not turn this failed delivery into PASSED.
        dest_delivery_path = run_dir / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_delivery_path.is_file())
        dest_data = json.loads(dest_delivery_path.read_text(encoding="utf-8"))
        self.assertEqual(dest_data["status"], DeliveryStatus.IN_PROGRESS)
        self.assertEqual(dest_data["attempts"][-1]["status"], DeliveryStatus.IN_PROGRESS)
        self.assertNotEqual(evaluate_run(LoadedRunPackage.load(run_dir)).status, "PASSED")

    def test_write_delivery_manifest_handles_collision_and_retries_safely(self):
        """When the first candidate temporary file collides, safely retry with a new UUID candidate."""
        manifest = DeliveryManifest(
            run_id="run-retry-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[
                DeliveryAttempt(
                    attempt_number=1,
                    command="run",
                    started_at_utc="2026-01-01T00:00:00Z",
                    destination=str(self.output),
                    status=DeliveryStatus.COMPLETED,
                )
            ],
        )
        dest_manifest = self.output / "delivery.json"

        occupied_uuid, free_uuid = UUID(int=1), UUID(int=2)
        occupied = self.output / f".delivery.json.tmp.{occupied_uuid.hex}"
        occupied.write_bytes(b"ANOTHER WRITER'S TEMPORARY FILE")

        with patch("uuid.uuid4", side_effect=[occupied_uuid, free_uuid]) as candidates:
            result_path = write_delivery_manifest(manifest, dest_manifest)

        self.assertEqual(result_path, dest_manifest)
        self.assertTrue(dest_manifest.is_file())
        self.assertEqual(candidates.call_count, 2)
        self.assertEqual(occupied.read_bytes(), b"ANOTHER WRITER'S TEMPORARY FILE")
        loaded = DeliveryManifest.from_dict(json.loads(dest_manifest.read_text(encoding="utf-8")))
        self.assertEqual(loaded.run_id, "run-retry-test")

    def test_write_delivery_manifest_collision_exhaustion_raises_path_safety_error(self):
        """If 10 successive temporary file candidates collide, raise PathSafetyError."""
        manifest = DeliveryManifest(
            run_id="run-exhaustion-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        dest_manifest = self.output / "delivery.json"

        occupied_uuid = UUID(int=1)
        occupied = self.output / f".delivery.json.tmp.{occupied_uuid.hex}"
        occupied.write_bytes(b"ANOTHER WRITER'S TEMPORARY FILE")

        with patch("uuid.uuid4", return_value=occupied_uuid) as candidates:
            with self.assertRaises(PathSafetyError) as ctx:
                write_delivery_manifest(manifest, dest_manifest)
            self.assertIn("Failed to allocate a unique temporary", str(ctx.exception))
        self.assertEqual(candidates.call_count, 10)
        self.assertFalse(dest_manifest.exists())
        self.assertEqual(occupied.read_bytes(), b"ANOTHER WRITER'S TEMPORARY FILE")

    def test_write_delivery_manifest_refuses_preplanted_symlink_candidate_and_preserves_external_sentinel(self):
        """Refuse to follow or unlink pre-planted symlink candidates."""
        sentinel = self.outside / "vital_secret.txt"
        sentinel.write_text("VITAL_SECRET_DO_NOT_TOUCH", encoding="utf-8")

        manifest = DeliveryManifest(
            run_id="run-preplanted-candidate-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        dest_manifest = self.output / "delivery.json"

        planted_symlink = self.output / ".delivery.json.tmp.planted_symlink"
        planted_symlink.symlink_to(sentinel)

        with patch("uuid.uuid4", return_value=type("FakeUUID", (), {"hex": "planted_symlink"})()):
            with self.assertRaises(PathSafetyError) as ctx:
                write_delivery_manifest(manifest, dest_manifest)
            self.assertIn("candidate is a symlink", str(ctx.exception).lower())

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "VITAL_SECRET_DO_NOT_TOUCH")
        self.assertTrue(planted_symlink.is_symlink())

    def test_write_delivery_manifest_contract_matches_production_caller(self):
        """Production caller pattern (run_dir / filename, package_root=run_dir) succeeds."""
        manifest = DeliveryManifest(
            run_id="run-contract-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        rel_run_dir = Path(os.path.relpath(self.root / "rel_output" / "run-001", Path.cwd()))
        target = rel_run_dir / DELIVERY_MANIFEST_FILENAME

        result_path = write_delivery_manifest(manifest, target, package_root=rel_run_dir)
        self.assertEqual(result_path, target)
        self.assertTrue(target.is_file())

    def test_write_delivery_manifest_rejects_target_escaping_package_root(self):
        """When target resolves outside package_root, PathSafetyError is raised."""
        manifest = DeliveryManifest(
            run_id="run-escape-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        pkg_root = self.output / "pkg_root"
        pkg_root.mkdir(parents=True, exist_ok=True)
        escaped_target = self.output / "escaped_delivery.json"

        with self.assertRaises(PathSafetyError) as ctx:
            write_delivery_manifest(manifest, escaped_target, package_root=pkg_root)
        self.assertIn("escapes package root", str(ctx.exception).lower())

    def test_write_delivery_manifest_refuses_symlink_in_relative_and_absolute_paths(self):
        """Refuses symlink destinations and package roots whether paths are absolute or relative."""
        manifest = DeliveryManifest(
            run_id="run-symlink-test",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        link_dir = self.output / "symlink_dir"
        link_dir.symlink_to(self.outside)

        abs_dest = link_dir / "delivery.json"
        with self.assertRaises(PathSafetyError):
            write_delivery_manifest(manifest, abs_dest)

        cwd_rel = os.path.relpath(str(self.output), os.getcwd())
        rel_dir = Path(cwd_rel) / "rel_symlink_dir"
        try:
            rel_dir.symlink_to(self.outside)
            rel_link = rel_dir / "delivery.json"
            with self.assertRaises(PathSafetyError):
                write_delivery_manifest(manifest, rel_link, package_root=rel_dir)
        finally:
            if rel_dir.is_symlink():
                rel_dir.unlink()

    def test_delivery_service_deliver_initial_success_and_failure_lifecycle(self):
        """Direct test of DeliveryService.deliver_initial on success and simulated failure."""
        run_id = "e2e-delivery-service-initial"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )

        delivery_stage = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage.read_text(encoding="utf-8")))

        # Test 1: Successful initial delivery
        res = DeliveryService.deliver_initial(stage_dir, run_dir, delivery)
        self.assertTrue(res.success)
        self.assertEqual(res.graded_root, run_dir)
        self.assertIsNone(res.export_failure)
        self.assertIsNone(res.ledger_failure)
        self.assertEqual(res.manifest.status, DeliveryStatus.COMPLETED)
        self.assertEqual(res.manifest.attempts[0].status, DeliveryStatus.COMPLETED)
        self.assertIsNotNone(res.manifest.attempts[0].completed_at_utc)

        # Verify disk facts: re-read destination delivery.json from disk
        dest_file = run_dir / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_file.is_file())
        dest_data = json.loads(dest_file.read_text(encoding="utf-8"))
        self.assertEqual(dest_data["status"], DeliveryStatus.COMPLETED)
        self.assertEqual(dest_data["attempts"][-1]["status"], DeliveryStatus.COMPLETED)
        self.assertIsNotNone(dest_data["attempts"][-1]["completed_at_utc"])
        self.assertIsNone(dest_data["attempts"][-1]["error"])

        # Also verify public report exit code is 0 on the delivered package
        messages_rep: list[str] = []
        _, args_rep = parse_e2e_args(["report", "--run", str(run_dir)])
        exit_rep = cmd_report(args_rep, emit=messages_rep.append)
        self.assertEqual(exit_rep, 0, msg=f"Report failed on delivered package: {messages_rep}")

        # Test 2: Simulated export failure
        fail_run_dir = self.output / f"{run_id}-fail"
        fail_stage_dir = run_stage_dir(self.workspace, f"{run_id}-fail")
        setup_baseline_package(
            fail_stage_dir, scenarios=["go-boundary"], run_id=f"{run_id}-fail",
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )
        fail_delivery = DeliveryManifest.from_dict(
            json.loads((fail_stage_dir / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        )
        fail_delivery.attempts[0].destination = str(fail_run_dir)
        self.assertIsNone(fail_delivery.attempts[0].completed_at_utc)
        self.assertIsNone(fail_delivery.attempts[0].error)

        with patch("ops.a8.e2e.delivery.export_run_package", side_effect=RuntimeError("Disk full")) as mock_export:
            res_fail = DeliveryService.deliver_initial(fail_stage_dir, fail_run_dir, fail_delivery)
        mock_export.assert_called_once()
        self.assertFalse(res_fail.success)
        self.assertEqual(res_fail.graded_root, fail_stage_dir)
        self.assertIsNotNone(res_fail.export_failure)
        self.assertIn("Disk full", str(res_fail.export_failure))
        self.assertEqual(res_fail.manifest.status, DeliveryStatus.FAILED)
        self.assertEqual(res_fail.manifest.attempts[0].status, DeliveryStatus.FAILED)
        for directory in (fail_stage_dir, fail_run_dir):
            persisted = json.loads((directory / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], DeliveryStatus.FAILED)
            attempt = persisted["attempts"][-1]
            self.assertEqual(attempt["status"], DeliveryStatus.FAILED)
            self.assertTrue(attempt["completed_at_utc"])
            self.assertEqual(attempt["error"], "Disk full")

    def test_failed_recovery_persists_a_completed_failure_and_preserves_prior_attempts(self):
        stage = self.workspace / "recovery-failure"
        destination = self.output / "recovery-failure"
        setup_baseline_package(
            stage, scenarios=["go-boundary"], run_id="recovery-failure",
            delivery_status=DeliveryStatus.COMPLETED,
        )
        ledger_path = stage / DELIVERY_MANIFEST_FILENAME
        before = json.loads(ledger_path.read_text(encoding="utf-8"))
        failure = OSError("Synthetic recovery export failure")
        with patch("ops.a8.e2e.delivery.export_run_package", side_effect=failure) as exporter:
            result = DeliveryService.deliver_recovery(stage, destination, suite_id="recovery-failure")
        exporter.assert_called_once()
        self.assertFalse(result.success)
        self.assertIs(result.export_failure, failure)
        self.assertIsNone(result.ledger_failure)
        self.assertEqual(result.graded_root, stage)
        for directory, expected_status in ((stage, before["status"]), (destination, DeliveryStatus.FAILED)):
            persisted = json.loads((directory / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
            # Recovery appends history but does not rewrite the original delivery status.
            self.assertEqual(persisted["status"], expected_status)
            self.assertEqual(persisted["attempts"][:-1], before["attempts"])
            attempt = persisted["attempts"][-1]
            self.assertEqual(attempt["attempt_number"], len(before["attempts"]) + 1)
            self.assertEqual(attempt["command"], "recover")
            self.assertEqual(attempt["destination"], str(destination))
            self.assertEqual(attempt["status"], DeliveryStatus.FAILED)
            self.assertTrue(attempt["completed_at_utc"])
            self.assertEqual(attempt["error"], str(failure))

    def test_initial_delivery_dual_failure_preserves_both_export_and_ledger_errors(self):
        """When initial delivery encounters both an export error and a staging ledger error,

        DeliveryService.deliver_initial returns both errors on DeliveryResult and export_and_record_delivery propagates them.
        """
        run_id = "e2e-init-dual-failure"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.IN_PROGRESS)

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))

        export_err = OSError("Initial export failed")
        ledger_err = OSError("Staging ledger write failed")

        real_write = DeliveryManifest.write
        export_called = [False]

        def faulty_exporter(*args, **kwargs):
            export_called[0] = True
            raise export_err

        def faulty_write(self_manifest, target, **kwargs):
            if Path(target) == delivery_stage_path and self_manifest.status == DeliveryStatus.FAILED:
                raise ledger_err
            return real_write(self_manifest, target, **kwargs)

        with patch("ops.a8.e2e.delivery.export_run_package", side_effect=faulty_exporter), \
             patch.object(DeliveryManifest, "write", faulty_write):
            graded_root, exp_fail, ledg_fail = export_and_record_delivery(stage_dir, run_dir, delivery)
            self.assertTrue(export_called[0], "Fault boundary exporter was not called")
            self.assertEqual(exp_fail, export_err)
            self.assertEqual(ledg_fail, ledger_err)
            self.assertEqual(graded_root, stage_dir)

    def test_initial_delivery_destination_creation_failure_records_failed_attempt_and_grades_stage(self):
        """When safe destination creation fails, attempt is FAILED and stage is graded. All fixtures are synthetic. No network, Docker, or live chain calls."""
        run_id = "e2e-init-mkdir-fail"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.IN_PROGRESS)

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))

        real_ensure = delivery_module.ensure_directory_safe
        creation_called = [False]

        def faulty_creation(path, *args, **kwargs):
            if path == run_dir:
                creation_called[0] = True
                raise PermissionError("Permission denied: output directory cannot be created")
            return real_ensure(path, *args, **kwargs)

        with patch.object(delivery_module, "ensure_directory_safe", faulty_creation):
            res = DeliveryService.deliver_initial(stage_dir, run_dir, delivery)

        self.assertTrue(creation_called[0], "safe directory creation was not called")
        self.assertFalse(res.success)
        self.assertEqual(res.graded_root, stage_dir)
        self.assertIsInstance(res.export_failure, PermissionError)
        self.assertIn("Permission denied", str(res.export_failure))

        # Staging ledger must be marked FAILED
        delivery_after = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_after.status, DeliveryStatus.FAILED)
        self.assertEqual(delivery_after.attempts[0].status, DeliveryStatus.FAILED)
        self.assertIn("Permission denied", str(delivery_after.attempts[0].error))

        # Output directory was never populated with a partial manifest
        self.assertFalse((run_dir / DELIVERY_MANIFEST_FILENAME).exists())

    def test_initial_delivery_first_destination_marker_failure_records_failed_attempt_and_grades_stage(self):
        """When initial destination marker write fails, attempt is marked FAILED and stage is graded."""
        run_id = "e2e-init-first-marker-fail"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.IN_PROGRESS)

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))

        real_write = DeliveryManifest.write
        marker_called = [False]

        def faulty_write(self_manifest, target, **kwargs):
            if Path(target) == run_dir / DELIVERY_MANIFEST_FILENAME and self_manifest.status == DeliveryStatus.IN_PROGRESS:
                marker_called[0] = True
                raise OSError("Disk full writing initial destination marker")
            return real_write(self_manifest, target, **kwargs)

        with patch.object(DeliveryManifest, "write", faulty_write):
            res = DeliveryService.deliver_initial(stage_dir, run_dir, delivery)

        self.assertTrue(marker_called[0], "faulty_write was not called for destination marker")
        self.assertFalse(res.success)
        self.assertEqual(res.graded_root, stage_dir)
        self.assertIsInstance(res.export_failure, OSError)
        self.assertIn("Disk full writing initial destination marker", str(res.export_failure))

        # Staging ledger must be updated to FAILED with the error
        delivery_after = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_after.status, DeliveryStatus.FAILED)
        self.assertEqual(delivery_after.attempts[0].status, DeliveryStatus.FAILED)
        self.assertIn("Disk full writing initial destination marker", str(delivery_after.attempts[0].error))

    def test_initial_delivery_dual_failure_destination_marker_and_stage_ledger_error(self):
        """When initial destination marker fails and updating staging ledger also fails, both errors are captured."""
        run_id = "e2e-init-dual-dest-marker-fail"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.IN_PROGRESS)

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))

        dest_err = OSError("Destination disk full during initial marker")
        stage_err = OSError("Staging disk read-only")

        real_write = DeliveryManifest.write
        dest_called = [False]
        stage_called = [False]

        def faulty_write(self_manifest, target, **kwargs):
            if Path(target) == run_dir / DELIVERY_MANIFEST_FILENAME and self_manifest.status == DeliveryStatus.IN_PROGRESS:
                dest_called[0] = True
                raise dest_err
            if Path(target) == delivery_stage_path and self_manifest.status == DeliveryStatus.FAILED:
                stage_called[0] = True
                raise stage_err
            return real_write(self_manifest, target, **kwargs)

        with patch.object(DeliveryManifest, "write", faulty_write):
            graded_root, exp_fail, ledg_fail = export_and_record_delivery(stage_dir, run_dir, delivery)

        self.assertTrue(dest_called[0], "Destination marker fault was not called")
        self.assertTrue(stage_called[0], "Staging ledger fault was not called")
        self.assertEqual(graded_root, stage_dir)
        self.assertEqual(exp_fail, dest_err)
        self.assertEqual(ledg_fail, stage_err)

    def test_a_foreign_destination_keeps_all_its_bytes_and_passing_verdict_after_delivery_conflict(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                stage = self.workspace / f"stage-{initial}"
                dest = self.output / f"dest-{initial}"
                setup_baseline_package(
                    stage, scenarios=["go-boundary"], run_id="source",
                    delivery_status=DeliveryStatus.IN_PROGRESS if initial else DeliveryStatus.COMPLETED,
                )
                setup_baseline_package(dest, scenarios=["go-boundary"], run_id="foreign", delivery_status=DeliveryStatus.COMPLETED)
                before = {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
                self.assertEqual(evaluate_run(LoadedRunPackage.load(dest)).status, "PASSED")
                if initial:
                    manifest = DeliveryManifest.from_dict(json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text()))
                    result = DeliveryService.deliver_initial(stage, dest, manifest)
                else:
                    result = DeliveryService.deliver_recovery(stage, dest, suite_id="source")
                self.assertIsInstance(result.export_failure, ExportConflict)
                self.assertFalse(result.success)
                self.assertEqual(before, {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()})
                self.assertEqual(evaluate_run(LoadedRunPackage.load(dest)).status, "PASSED")
                ledger = json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text())
                self.assertEqual(ledger["attempts"][-1]["status"], DeliveryStatus.FAILED)

    def test_matching_run_id_does_not_allow_replacing_a_ledger_when_execution_identity_differs(self):
        stage, dest = self.workspace / "stage", self.output / "dest"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source", delivery_status=DeliveryStatus.COMPLETED)
        self.assertTrue(DeliveryService.deliver_recovery(stage, dest, suite_id="source").success)
        execution_path = dest / EXECUTION_MANIFEST_FILENAME
        execution = json.loads(execution_path.read_text())
        # Matching run IDs alone must not authorize replacing the ledger.
        recorded_hash = execution["build_manifest_sha256"]
        execution["build_manifest_sha256"] = ("0" if recorded_hash[0] != "0" else "1") + recorded_hash[1:]
        execution_path.write_text(json.dumps(execution))
        before = (dest / DELIVERY_MANIFEST_FILENAME).read_bytes()
        result = DeliveryService.deliver_recovery(stage, dest, suite_id="source")
        self.assertIsInstance(result.export_failure, ExportConflict)
        self.assertEqual((dest / DELIVERY_MANIFEST_FILENAME).read_bytes(), before)

    def test_recovery_publishes_a_completed_operational_status_after_copying_evidence(self):
        stage, dest = self.workspace / "stage-status", self.output / "dest-status"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source", delivery_status=DeliveryStatus.FAILED)
        (stage / "status.json").write_text(json.dumps({
            "schema_version": "e2e/run-status/1",
            "run_id": "source",
            "state": "completed",
            "current_stage": "exporting",
            "export_status": "IN_PROGRESS",
        }) + "\n", encoding="utf-8")

        result = DeliveryService.deliver_recovery(stage, dest, suite_id="source")
        self.assertTrue(result.success)
        for root in (stage, dest):
            with self.subTest(root=root):
                status = json.loads((root / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["export_status"], "COMPLETED")

    def test_a_foreign_ledger_in_an_otherwise_empty_destination_is_preserved(self):
        stage, dest = self.workspace / "stage", self.output / "dest"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source", delivery_status=DeliveryStatus.COMPLETED)
        manifest = DeliveryManifest.from_dict(json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text()))
        manifest.run_id = "foreign"
        dest.mkdir()
        manifest.write(dest / DELIVERY_MANIFEST_FILENAME, package_root=dest)
        before = (dest / DELIVERY_MANIFEST_FILENAME).read_bytes()
        result = DeliveryService.deliver_recovery(stage, dest, suite_id="source")
        self.assertIsInstance(result.export_failure, ExportConflict)
        self.assertEqual((dest / DELIVERY_MANIFEST_FILENAME).read_bytes(), before)
        self.assertEqual(list(dest.iterdir()), [dest / DELIVERY_MANIFEST_FILENAME])

    def test_recovery_rejects_a_foreign_ledger_inside_the_staging_package(self):
        """A staging ledger from another run must not acquire a recovery attempt. All fixtures are synthetic. No network, Docker, or live chain calls."""
        stage, dest = self.workspace / "foreign-stage", self.output / "foreign-stage-dest"
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id="source")
        ledger_path = stage / DELIVERY_MANIFEST_FILENAME
        ledger = DeliveryManifest.from_dict(json.loads(ledger_path.read_text(encoding="utf-8")))
        ledger.run_id = "foreign"
        ledger.write(ledger_path, package_root=stage)
        before = ledger_path.read_bytes()

        with self.assertRaises(CorruptDeliveryLedgerError):
            DeliveryService.deliver_recovery(stage, dest, suite_id="source")
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertFalse(dest.exists())

    def test_a_concurrent_recovery_is_rejected_before_reading_the_ledger_and_retry_preserves_history(self):
        for initial in (False, True):
            with self.subTest(initial=initial):
                stage = self.workspace / f"stage-{initial}"
                dest = self.output / f"dest-{initial}"
                retry_dest = self.output / f"retry-{initial}"
                setup_baseline_package(
                    stage, scenarios=["go-boundary"], run_id="source",
                    delivery_status=DeliveryStatus.IN_PROGRESS if initial else DeliveryStatus.COMPLETED,
                )
                manifest = DeliveryManifest.from_dict(json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text()))
                if initial:
                    manifest.attempts[0].destination = str(dest)
                entered, release = threading.Event(), threading.Event()

                def paused_exporter(*args, **kwargs):
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError("Test did not release the export boundary")
                    return export_run_package(*args, **kwargs)

                with ThreadPoolExecutor(max_workers=1) as pool:
                    if initial:
                        future = pool.submit(DeliveryService.deliver_initial, stage, dest, manifest, exporter=paused_exporter)
                    else:
                        future = pool.submit(DeliveryService.deliver_recovery, stage, dest, suite_id="source", exporter=paused_exporter)
                    try:
                        self.assertTrue(entered.wait(10))
                        before = (stage / DELIVERY_MANIFEST_FILENAME).read_bytes()
                        original_open = Path.open
                        competing_thread = threading.get_ident()
                        ledger_reads = []

                        def track_competing_read(path, mode="r", *args, **kwargs):
                            if (threading.get_ident() == competing_thread
                                    and path == stage / DELIVERY_MANIFEST_FILENAME
                                    and ("r" in mode or "+" in mode)):
                                ledger_reads.append(path)
                            return original_open(path, mode, *args, **kwargs)

                        # Scope instrumentation to the competing call, not the
                        # active worker or the assertions' own ledger reads.
                        with patch.object(Path, "open", track_competing_read):
                            with self.assertRaises(DeliveryBusyError):
                                DeliveryService.deliver_recovery(stage, retry_dest, suite_id="source")
                        self.assertEqual(ledger_reads, [], "Busy recovery read the ledger before acquiring its lock")
                        self.assertEqual((stage / DELIVERY_MANIFEST_FILENAME).read_bytes(), before)
                        self.assertFalse(retry_dest.exists())
                    finally:
                        release.set()
                    self.assertTrue(future.result(timeout=10).success)
                completed_history = json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text())["attempts"]
                self.assertTrue(DeliveryService.deliver_recovery(stage, retry_dest, suite_id="source").success)
                ledger = json.loads((stage / DELIVERY_MANIFEST_FILENAME).read_text())
                self.assertEqual(ledger["attempts"][:-1], completed_history)
                expected = [1, 2] if initial else [1, 2, 3]
                self.assertEqual([a["attempt_number"] for a in ledger["attempts"]], expected)
                self.assertTrue(all(a["status"] == DeliveryStatus.COMPLETED for a in ledger["attempts"]))
