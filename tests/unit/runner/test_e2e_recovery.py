"""Tests proving robust recovery lifecycle, state transitions, and ledger preservation.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forward_e2e.execution import delivery as delivery_module
from forward_e2e.execution.cli import cmd_recover, cmd_report, parse_e2e_args
from forward_e2e.execution.errors import ExportConflict
from forward_e2e.execution.executor import export_and_record_delivery
from forward_e2e.execution.file_safety import sha256_file
from forward_e2e.execution.gitio import GitClient
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    DeliveryManifest,
    DeliveryStatus,
    content_sha256,
    write_run_lock,
)
from forward_e2e.execution.runpackage import (
    RUN_RESULT_FILENAME,
    run_stage_dir,
)
from forward_e2e.execution.sources import SourceAcquirer, bundle_ref
from tests.unit.runner.support.fakes import CONTRACTS_SHA, GONKA_SHA
from tests.unit.runner.support.packages import (
    make_baseline_lock,
    make_build_manifest,
    setup_baseline_package,
)


class TestE2ERecovery(unittest.TestCase):
    """Proves the safety, isolation, and immutability of the recover workflow."""

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

    def test_recovery_rejects_output_nested_in_staging_before_recording_attempt(self):
        stage = self.workspace / "run-overlap" / "run"
        stage.mkdir(parents=True)
        ledger = stage / DELIVERY_MANIFEST_FILENAME
        ledger.write_text('{"sentinel":"unchanged"}', encoding="utf-8")
        destination = stage / "export"

        with self.assertRaises(ExportConflict):
            delivery_module.DeliveryService.deliver_recovery(
                stage, destination, suite_id="run-overlap"
            )

        self.assertEqual(ledger.read_text(encoding="utf-8"), '{"sentinel":"unchanged"}')
        self.assertFalse(destination.exists())

    def test_recover_rejects_symlink_delivery_manifest_leaf_in_destination_and_preserves_sentinel(self):
        """Recovery refuses to overwrite a destination delivery.json that is a symlink."""
        sentinel = self.outside / "sentinel.txt"
        sentinel.write_text("OUTSIDE_UNTOUCHED", encoding="utf-8")

        run_id = "e2e-rec-symlink-leaf"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        dest_dir = self.output / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        symlink_dest = dest_dir / DELIVERY_MANIFEST_FILENAME
        symlink_dest.symlink_to(sentinel)

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertTrue(any("symlink" in m.lower() for m in messages))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "OUTSIDE_UNTOUCHED")

    def test_recover_rejects_dangling_symlink_delivery_manifest_in_destination(self):
        """Recovery refuses a dangling symlink delivery.json in destination."""
        run_id = "e2e-rec-dangling-symlink"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        dest_dir = self.output / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dangling = dest_dir / DELIVERY_MANIFEST_FILENAME
        dangling.symlink_to(self.outside / "non_existent_target.txt")

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertTrue(any("symlink" in m.lower() for m in messages))

    def test_recover_rejects_symlink_ancestor_directory_in_destination(self):
        """Recovery refuses when destination run directory traverses a symlinked directory."""
        sentinel_dir = self.outside / "infiltrated"
        sentinel_dir.mkdir(parents=True, exist_ok=True)

        symlink_parent = self.output / "symlink_dir"
        symlink_parent.symlink_to(sentinel_dir)

        run_id = "e2e-rec-symlink-ancestor"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(symlink_parent), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertTrue(any("symlink" in m.lower() for m in messages))

    def test_initial_export_failure_does_not_mutate_staged_execution_manifest_and_allows_repeat_recover(self):
        """Failure during initial export leaves staged execution manifest byte-for-byte immutable."""
        run_id = "e2e-rec-export-failure-repeat"
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
        delivery = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))

        with patch("forward_e2e.execution.delivery.export_run_package", side_effect=OSError("Simulated host IO failure")) as mock_export:
            graded_root, failure, ledger_failure = export_and_record_delivery(stage_dir, run_dir, delivery)

        mock_export.assert_called_once()
        self.assertEqual(graded_root, stage_dir)
        self.assertIsNotNone(failure)
        self.assertEqual(exec_manifest_stage.read_bytes(), manifest_bytes_before)

        delivery_after = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_after.status, DeliveryStatus.FAILED)
        self.assertEqual(delivery_after.attempts[0].status, DeliveryStatus.FAILED)

        messages_rec: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages_rec.append)
        self.assertEqual(code, 0)

        delivery_recovered = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(len(delivery_recovered.attempts), 2)
        self.assertEqual(delivery_recovered.attempts[0].status, DeliveryStatus.FAILED)
        self.assertEqual(delivery_recovered.attempts[1].status, DeliveryStatus.COMPLETED)
        self.assertIsNotNone(delivery_recovered.attempts[1].completed_at_utc)
        self.assertIsNone(delivery_recovered.attempts[1].error)

        # Verify disk facts: re-read destination delivery.json from disk
        dest_file = run_dir / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_file.is_file())
        dest_data = json.loads(dest_file.read_text(encoding="utf-8"))
        self.assertEqual(dest_data["status"], DeliveryStatus.COMPLETED)
        self.assertEqual(len(dest_data["attempts"]), 2)
        self.assertEqual(dest_data["attempts"][0]["status"], DeliveryStatus.FAILED)
        self.assertEqual(dest_data["attempts"][1]["status"], DeliveryStatus.COMPLETED)
        self.assertIsNotNone(dest_data["attempts"][1]["completed_at_utc"])
        self.assertIsNone(dest_data["attempts"][1]["error"])

        # Also verify public report exit code is 0 on the recovered run package
        messages_rep: list[str] = []
        _, args_rep = parse_e2e_args(["report", "--run", str(run_dir)])
        exit_rep = cmd_report(args_rep, emit=messages_rep.append)
        self.assertEqual(exit_rep, 0, msg=f"Report failed on recovered package: {messages_rep}")

    def test_corrupt_existing_ledger_in_staging_during_recover_refuses_silently_overwriting_history(self):
        """When delivery.json in staging is corrupt, cmd_recover refuses and does not erase history."""
        run_id = "e2e-rec-corrupt-staging-ledger"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        (stage_dir / DELIVERY_MANIFEST_FILENAME).write_text("CORRUPTED_JSON_NOT_VALID", encoding="utf-8")

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertTrue(any("unreadable or corrupted" in m for m in messages))
        self.assertEqual((stage_dir / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"), "CORRUPTED_JSON_NOT_VALID")

    def test_public_recover_succeeds_with_stale_pid_tempfile_in_stage_and_destination(self):
        """Recovery succeeds even if stale tempfiles from hard stops are left behind."""
        run_id = "e2e-rec-stale-tempfile"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        stale_stage_tmp = stage_dir / ".delivery.json.tmp.99999"
        stale_stage_tmp.write_text("STALE_STAGE_TMP", encoding="utf-8")

        dest_dir = self.output / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        stale_dest_tmp = dest_dir / ".delivery.json.tmp.99999"
        stale_dest_tmp.write_text("STALE_DEST_TMP", encoding="utf-8")

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 0)
        self.assertTrue((dest_dir / DELIVERY_MANIFEST_FILENAME).is_file())
        self.assertEqual(stale_stage_tmp.read_text(encoding="utf-8"), "STALE_STAGE_TMP")
        self.assertEqual(stale_dest_tmp.read_text(encoding="utf-8"), "STALE_DEST_TMP")

        dest_data = json.loads((dest_dir / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(dest_data["status"], DeliveryStatus.COMPLETED)
        self.assertEqual(dest_data["attempts"][-1]["status"], DeliveryStatus.COMPLETED)
        self.assertIsNotNone(dest_data["attempts"][-1]["completed_at_utc"])
        self.assertIsNone(dest_data["attempts"][-1]["error"])

        # First report on destination produces PASSED
        messages_rep1: list[str] = []
        _, args_rep1 = parse_e2e_args(["report", "--run", str(dest_dir)])
        exit_rep1 = cmd_report(args_rep1, emit=messages_rep1.append)
        self.assertEqual(exit_rep1, 0, msg=f"First report failed: {messages_rep1}")

        # Second recovery into same output directory is idempotent
        messages_rec2: list[str] = []
        exit_rec2 = cmd_recover(args, emit=messages_rec2.append)
        self.assertEqual(exit_rec2, 0)

        # Second report on destination also produces PASSED
        messages_rep2: list[str] = []
        exit_rep2 = cmd_report(args_rep1, emit=messages_rep2.append)
        self.assertEqual(exit_rep2, 0, msg=f"Second report failed: {messages_rep2}")

    def test_public_recover_with_relative_output_and_absolute_workspace(self):
        """cmd_recover handles relative --output and absolute --workspace without errors."""
        run_id = "e2e-rec-rel-output-abs-ws"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        cwd_rel = os.path.relpath(str(self.output), os.getcwd())
        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", cwd_rel, "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 0)
        dest_file = self.output / run_id / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_file.is_file())

    def test_public_recover_with_relative_output_and_relative_workspace(self):
        """cmd_recover handles relative --output and relative --workspace."""
        run_id = "e2e-rec-rel-output-rel-ws"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        cwd_rel_out = os.path.relpath(str(self.output), os.getcwd())
        cwd_rel_ws = os.path.relpath(str(self.workspace), os.getcwd())
        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", cwd_rel_out, "--workspace", cwd_rel_ws]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 0)
        dest_file = self.output / run_id / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_file.is_file())

    def test_export_failure_persists_across_report_and_recovers_cleanly_into_new_output(self):
        """A package whose export failed reports failed and recovers cleanly to a new destination."""
        run_id = "e2e-rec-clean-new-dest"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(
            stage_dir,
            scenarios=["go-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.FAILED,
            export_error="Network down",
        )

        messages_rep: list[str] = []
        argv_rep = ["report", "--run", str(stage_dir)]
        _, args_rep = parse_e2e_args(argv_rep)
        rep_code = cmd_report(args_rep, emit=messages_rep.append)
        self.assertEqual(rep_code, 1)

        new_out = self.root / "recovered-new-dest"
        messages_rec: list[str] = []
        argv_rec = ["recover", "--run", run_id, "--output", str(new_out), "--workspace", str(self.workspace)]
        _, args_rec = parse_e2e_args(argv_rec)
        rec_code = cmd_recover(args_rec, emit=messages_rec.append)
        self.assertEqual(rec_code, 0)

        dest_manifest = new_out / run_id / DELIVERY_MANIFEST_FILENAME
        self.assertTrue(dest_manifest.is_file())
        dest_delivery = DeliveryManifest.from_dict(json.loads(dest_manifest.read_text(encoding="utf-8")))
        self.assertEqual(dest_delivery.status, DeliveryStatus.COMPLETED)
        self.assertEqual(dest_delivery.attempts[-1].status, DeliveryStatus.COMPLETED)

    def test_full_lifecycle_and_repeated_recovery_into_same_output(self):
        """Repeated recovery into the same output directory succeeds without conflict."""
        run_id = "e2e-rec-idempotent-repeat"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.COMPLETED)

        for _ in range(2):
            messages: list[str] = []
            argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
            _, args = parse_e2e_args(argv)
            code = cmd_recover(args, emit=messages.append)
            self.assertEqual(code, 0)

    def test_tampered_destination_artifact_triggers_export_conflict_on_repeat_recovery(self):
        """When an immutable destination artifact has different bytes, recovery refuses with conflict."""
        run_id = "e2e-rec-tampered-conflict"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        # 1. First recovery succeeds
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        self.assertEqual(cmd_recover(args, emit=lambda m: None), 0)

        # 2. Tamper with lock file in destination
        dest_lock = self.output / run_id / RUN_LOCK_FILENAME
        dest_lock.write_text("TAMPERED_LOCK_DIFFERENT_BYTES", encoding="utf-8")

        # 3. Second recovery detects conflict and refuses
        messages: list[str] = []
        code2 = cmd_recover(args, emit=messages.append)
        self.assertEqual(code2, 1)
        self.assertTrue(any("Destination contains a different run package." in m for m in messages))
        self.assertEqual(dest_lock.read_text(encoding="utf-8"), "TAMPERED_LOCK_DIFFERENT_BYTES")

    def test_recovery_ignores_leftover_owned_temporary_files_from_hard_stop(self):
        """Temporary files created by aborted exports do not block subsequent recovery."""
        run_id = "e2e-rec-leftover-tmp"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        dest_dir = self.output / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        leftover_tmp = dest_dir / ".run.lock.json.tmp.ab12cd34"
        leftover_tmp.write_text("OLD_ABORTED_STATE", encoding="utf-8")

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)
        self.assertEqual(code, 0)
        self.assertTrue((dest_dir / RUN_LOCK_FILENAME).is_file())

    def test_recovery_of_an_incomplete_package_preserves_durable_bundle_bytes(self):
        """Recovery exports stored bundles unchanged even when the run cannot pass grading."""
        run_id = "e2e-20260101-000000-r12crash"
        stage = run_stage_dir(self.workspace, run_id)
        stage.mkdir(parents=True, exist_ok=True)

        gonka_bytes = b"SYNTHETIC-GONKA-BUNDLE"
        contracts_bytes = b"SYNTHETIC-CONTRACTS-BUNDLE"
        g_hash = hashlib.sha256(gonka_bytes).hexdigest()
        c_hash = hashlib.sha256(contracts_bytes).hexdigest()

        lock = make_baseline_lock(
            ["wasm-abi-boundary"],
            gonka_bundle_sha256=g_hash,
            contracts_bundle_sha256=c_hash,
            gonka_bundle_ref=bundle_ref("gonka"),
            contracts_bundle_ref=bundle_ref("contracts"),
        )
        write_run_lock(lock, stage / RUN_LOCK_FILENAME)
        lock_hash = content_sha256(lock.to_dict())

        bundles_dir = stage / "bundles"
        bundles_dir.mkdir(parents=True, exist_ok=True)
        (bundles_dir / "gonka.bundle").write_bytes(gonka_bytes)
        (bundles_dir / "contracts.bundle").write_bytes(contracts_bytes)

        build_m = make_build_manifest(lock_sha256=lock_hash, run_id=run_id)
        (stage / BUILD_MANIFEST_FILENAME).write_text(json.dumps(build_m.to_dict(), indent=2), encoding="utf-8")

        _, rec_args = parse_e2e_args([
            "recover",
            "--run", run_id,
            "--workspace", str(self.workspace),
            "--output", str(self.output),
        ])
        rec_code = cmd_recover(rec_args, emit=lambda m: None)

        self.assertEqual(rec_code, 1)
        recovered_pkg = self.output / run_id
        result = json.loads((recovered_pkg / RUN_RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertTrue(recovered_pkg.is_dir())
        self.assertTrue((recovered_pkg / RUN_LOCK_FILENAME).is_file())
        self.assertTrue((recovered_pkg / BUILD_MANIFEST_FILENAME).is_file())
        self.assertTrue((recovered_pkg / "bundles" / "gonka.bundle").is_file())
        self.assertTrue((recovered_pkg / "bundles" / "contracts.bundle").is_file())

        def fake_git_proc(argv, **kwargs):
            if "bundle" in argv and "verify" in argv:
                return SimpleNamespace(returncode=0, stdout="The bundle records a complete history.\n", stderr="")
            if "ls-remote" in argv:
                bundle_p = str(argv[-1])
                role = "contracts" if "contracts" in bundle_p else "gonka"
                sha = CONTRACTS_SHA if role == "contracts" else GONKA_SHA
                return SimpleNamespace(returncode=0, stdout=f"{sha}\t{bundle_ref(role)}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        fake_client = GitClient(runner=fake_git_proc)
        acquirer = SourceAcquirer(
            git=fake_client,
            package_dir=recovered_pkg,
            scratch_dir=self.root / "scratch",
        )
        gonka_bundle = acquirer.verify_stored_bundle(lock.gonka, package_root=recovered_pkg)
        self.assertEqual(gonka_bundle, recovered_pkg / "bundles" / "gonka.bundle")
        self.assertEqual(sha256_file(gonka_bundle), g_hash)

        contracts_bundle = acquirer.verify_stored_bundle(lock.contracts, package_root=recovered_pkg)
        self.assertEqual(contracts_bundle, recovered_pkg / "bundles" / "contracts.bundle")
        self.assertEqual(sha256_file(contracts_bundle), c_hash)

    def test_partial_run_missing_execution_manifest_recovers_as_incomplete_with_nonzero_exit(self):
        run_id = "e2e-20260101-000000-r12noexec"
        stage = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage, scenarios=["go-boundary"], run_id=run_id)
        (stage / EXECUTION_MANIFEST_FILENAME).unlink()

        _, rec_args = parse_e2e_args([
            "recover",
            "--run", run_id,
            "--workspace", str(self.workspace),
            "--output", str(self.output),
        ])
        rec_code = cmd_recover(rec_args, emit=lambda m: None)
        self.assertEqual(rec_code, 1)

        result_file = self.output / run_id / RUN_RESULT_FILENAME
        self.assertTrue(result_file.is_file())
        res_data = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(res_data["status"], "INCOMPLETE")
        self.assertIn("INCOMPLETE_EXECUTION_MANIFEST_MISSING", [f["code"] for f in res_data["findings"]])

    def test_recovery_dual_failure_preserves_both_export_and_ledger_errors(self):
        """When recovery encounters both an export failure and a staging ledger update failure,

        both reasons are emitted; the destination attempt records the export error.
        """
        run_id = "e2e-rec-dual-failure"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        export_err = OSError("Destination disk write failed")
        ledger_err = OSError("Staging disk readonly")

        real_write = DeliveryManifest.write
        export_called = [False]

        def faulty_exporter(*args, **kwargs):
            export_called[0] = True
            raise export_err

        def faulty_write(self_manifest, target, **kwargs):
            # Allow the initial IN_PROGRESS write to staging and any destination writes
            # Fail only when saving the FAILED attempt into staging
            if Path(target) == stage_dir / DELIVERY_MANIFEST_FILENAME and self_manifest.attempts[-1].status == DeliveryStatus.FAILED:
                raise ledger_err
            return real_write(self_manifest, target, **kwargs)

        with patch("forward_e2e.execution.delivery.export_run_package", side_effect=faulty_exporter), \
             patch.object(DeliveryManifest, "write", faulty_write):
            messages: list[str] = []
            argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
            _, args = parse_e2e_args(argv)
            exit_code = cmd_recover(args, emit=messages.append)
            self.assertEqual(exit_code, 1)

            self.assertTrue(export_called[0], "Fault boundary exporter was never called!")
            self.assertTrue(any(
                "warning: failed to update delivery ledger" in m.lower()
                and str(ledger_err) in m for m in messages
            ))
            self.assertTrue(any("Destination disk write failed" in m for m in messages))

        destination_ledger = json.loads(
            (self.output / run_id / DELIVERY_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(destination_ledger["attempts"][-1]["status"], DeliveryStatus.FAILED)
        self.assertEqual(destination_ledger["attempts"][-1]["error"], str(export_err))

    def test_recovery_destination_creation_failure_records_failed_attempt_and_allows_repeat_recover(self):
        """Safe destination creation failure is recorded and recover can retry. All fixtures are synthetic. No network, Docker, or live chain calls."""
        run_id = "e2e-rec-mkdir-fail"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        run_dir = self.output / run_id
        real_ensure = delivery_module.ensure_directory_safe
        creation_called = [False]

        def faulty_creation(path, *args, **kwargs):
            if path == run_dir:
                creation_called[0] = True
                raise PermissionError("Permission denied creating recovery run directory")
            return real_ensure(path, *args, **kwargs)

        messages_fail: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)

        with patch.object(delivery_module, "ensure_directory_safe", faulty_creation):
            code = cmd_recover(args, emit=messages_fail.append)

        self.assertEqual(code, 1)
        self.assertTrue(creation_called[0], "safe directory creation was not called")
        self.assertTrue(any("Error recovering run package" in m for m in messages_fail))
        self.assertTrue(any("Permission denied creating recovery run directory" in m for m in messages_fail))
        self.assertFalse(any("Error recording recovery attempt in staging" in m for m in messages_fail))

        # Staging attempt must be marked FAILED with the real error reason
        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery_after = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_after.attempts[-1].status, DeliveryStatus.FAILED)
        self.assertIn("Permission denied creating recovery run directory", str(delivery_after.attempts[-1].error))

        # Repeat recover after resolving permission succeeds
        messages_ok: list[str] = []
        code_ok = cmd_recover(args, emit=messages_ok.append)
        self.assertEqual(code_ok, 0)
        delivery_ok = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_ok.attempts[-1].status, DeliveryStatus.COMPLETED)

    def test_recovery_first_destination_marker_write_failure_records_failed_attempt_and_allows_repeat_recover(self):
        """When writing initial destination marker fails during recovery, attempt is marked FAILED in staging and repeat recovery succeeds."""
        run_id = "e2e-rec-first-marker-fail"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        run_dir = self.output / run_id
        real_write = DeliveryManifest.write
        marker_called = [False]

        def faulty_write(self_manifest, target, **kwargs):
            if str(run_dir) in str(target) and self_manifest.status == DeliveryStatus.IN_PROGRESS:
                marker_called[0] = True
                raise OSError("Disk full writing recovery initial marker")
            return real_write(self_manifest, target, **kwargs)

        messages_fail: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)

        with patch.object(DeliveryManifest, "write", faulty_write):
            code = cmd_recover(args, emit=messages_fail.append)

        self.assertEqual(code, 1)
        self.assertTrue(marker_called[0], "faulty_write was not called for initial destination marker")
        self.assertTrue(any("Error recovering run package" in m for m in messages_fail))
        self.assertTrue(any("Disk full writing recovery initial marker" in m for m in messages_fail))
        self.assertFalse(any("Error recording recovery attempt in staging" in m for m in messages_fail))

        # Staging attempt must be marked FAILED
        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery_after = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_after.attempts[-1].status, DeliveryStatus.FAILED)
        self.assertIn("Disk full writing recovery initial marker", str(delivery_after.attempts[-1].error))

        # Subsequent recovery succeeds
        messages_ok: list[str] = []
        code_ok = cmd_recover(args, emit=messages_ok.append)
        self.assertEqual(code_ok, 0)
        delivery_ok = DeliveryManifest.from_dict(json.loads(delivery_stage_path.read_text(encoding="utf-8")))
        self.assertEqual(delivery_ok.attempts[-1].status, DeliveryStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
