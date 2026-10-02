"""Tests proving path safety, symlink defense, exclusive tempfile allocation, and atomic operations.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ops.a8.e2e import file_safety, runpackage
from ops.a8.e2e.cli import cmd_recover, cmd_report, parse_e2e_args
from ops.a8.e2e.errors import ExportConflict, IntegrityError, PathSafetyError
from ops.a8.e2e.executor import export_and_record_delivery, grade_run_package
from ops.a8.e2e.file_safety import (
    atomic_publish_file,
    atomic_write_bytes,
    sha256_file,
    sha256_size_checked_file,
)
from ops.a8.e2e.runlock import (
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    write_delivery_manifest,
)
from ops.a8.e2e.runpackage import export_run_package, run_stage_dir
from ops.a8.tests.support.packages import setup_baseline_package


class TestE2EPathSafety(unittest.TestCase):
    """Proves symlink defenses and low-level atomic filesystem safety."""

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

    def test_checked_digest_rejects_source_replaced_during_read(self):
        source = self.workspace / "artifact.log"
        source.write_bytes(b"first version")
        replacement = self.workspace / "replacement.log"
        replacement.write_bytes(b"second version with another size")
        real_source = file_safety._regular_source_file

        @contextmanager
        def replace_after_open(path):
            with real_source(path) as opened:
                source.rename(self.workspace / "original.log")
                replacement.rename(source)
                yield opened

        with patch("ops.a8.e2e.file_safety._regular_source_file", replace_after_open):
            with self.assertRaises((PathSafetyError, IntegrityError)):
                sha256_size_checked_file(source)

    def test_recover_rejects_symlink_ancestor_in_output_preserves_outside_sentinel_and_creates_nothing(self):
        """When an ancestor directory in the output path is a symlink, recovery refuses."""
        secret_dir = self.outside / "secret"
        secret_dir.mkdir(parents=True, exist_ok=True)
        outside_sentinel = secret_dir / "canary.txt"
        outside_sentinel.write_text("CANARY_SAFE", encoding="utf-8")

        symlink_base = self.output / "symlink_base"
        symlink_base.symlink_to(secret_dir)

        outside_before = sorted(p.relative_to(secret_dir) for p in secret_dir.rglob("*"))
        malicious_output = symlink_base / "nested" / "dest"
        run_id = "e2e-safety-symlink-ancestor"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(malicious_output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertTrue(any("symlink" in m.lower() for m in messages))
        self.assertEqual(outside_sentinel.read_text(encoding="utf-8"), "CANARY_SAFE")
        self.assertEqual(sorted(p.relative_to(secret_dir) for p in secret_dir.rglob("*")), outside_before)

    def test_recover_with_missing_run_id_in_symlinked_output_does_not_create_run_id_outside(self):
        """Passing an output root that is a symlink fails without creating any directories in target."""
        outside_root = self.outside / "trapped"
        outside_root.mkdir(parents=True, exist_ok=True)
        self.output.rmdir()
        self.output.symlink_to(outside_root)

        run_id = "e2e-safety-symlink-root"
        stage_dir = run_stage_dir(self.workspace, run_id)
        setup_baseline_package(stage_dir, scenarios=["go-boundary"], run_id=run_id, delivery_status=DeliveryStatus.FAILED)

        messages: list[str] = []
        argv = ["recover", "--run", run_id, "--output", str(self.output), "--workspace", str(self.workspace)]
        _, args = parse_e2e_args(argv)
        code = cmd_recover(args, emit=messages.append)

        self.assertEqual(code, 1)
        self.assertFalse((outside_root / run_id).exists())

    def test_write_delivery_manifest_rejects_preplanted_symlink_tempfile_leaving_sentinel_intact(self):
        """Refuse to follow preplanted symlinks for temporary files."""
        sentinel = self.outside / "target_canary.txt"
        sentinel.write_text("PRISTINE_DATA", encoding="utf-8")

        manifest = DeliveryManifest(
            run_id="e2e-safety-preplanted",
            status=DeliveryStatus.COMPLETED,
            attempts=[],
        )
        dest_manifest = self.output / "delivery.json"

        with patch("uuid.uuid4", return_value=type("FakeUUID", (), {"hex": "dummy_uuid"})()):
            planted_temp = self.output / ".delivery.json.tmp.dummy_uuid"
            planted_temp.symlink_to(sentinel)

            with self.assertRaises(PathSafetyError):
                write_delivery_manifest(manifest, dest_manifest)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "PRISTINE_DATA")

    def test_write_delivery_manifest_succeeds_for_normal_absolute_and_relative_paths(self):
        """write_delivery_manifest succeeds for standard absolute and relative destinations."""
        manifest = DeliveryManifest(
            run_id="e2e-safety-normal-paths",
            status=DeliveryStatus.COMPLETED,
            attempts=[
                DeliveryAttempt(
                    attempt_number=1,
                    command="run",
                    started_at_utc="2026-01-01T00:00:00Z",
                    destination=str(self.output / "abs"),
                    status=DeliveryStatus.COMPLETED,
                )
            ],
        )
        abs_dest = self.output / "abs" / "delivery.json"
        write_delivery_manifest(manifest, abs_dest)
        self.assertTrue(abs_dest.is_file())

        cwd_rel = os.path.relpath(str(self.output), os.getcwd())
        rel_dest = Path(cwd_rel) / "rel" / "delivery.json"
        write_delivery_manifest(manifest, rel_dest)
        self.assertTrue(rel_dest.is_file())

    def test_export_failure_mid_file_leaves_no_partial_destination_and_allows_double_recovery(self):
        """When an I/O fault occurs mid-file during streaming export of a large artifact:

        1. No partial/truncated destination file remains.
        2. Staging data is untouched.
        3. Subsequent recovery into the exact same output directory succeeds.
        4. Second recovery is idempotent.
        5. Report produces PASSED on the recovered run package.
        """
        run_id = "e2e-safety-mid-stream-failure"
        stage_dir = run_stage_dir(self.workspace, run_id)
        run_dir = self.output / run_id

        setup_baseline_package(
            stage_dir,
            scenarios=["wasm-abi-boundary"],
            run_id=run_id,
            delivery_status=DeliveryStatus.IN_PROGRESS,
        )

        large_artifact_stage = stage_dir / "large_evidence_bundle.bin"
        large_data = b"LARGE_PAYLOAD_CHUNK_ABCD" * 1024 * 64  # ~1.5 MB
        large_artifact_stage.write_bytes(large_data)
        stage_sha_before = hashlib.sha256(large_data).hexdigest()

        large_artifact_dest = run_dir / "large_evidence_bundle.bin"

        real_source = file_safety._regular_source_file
        fault_injected = [False]

        class FaultyFileWrapper:
            def __init__(self, f, target_name):
                self._f = f
                self._target_name = target_name
                self._read_count = 0

            def read(self, *args, **kwargs):
                chunk = self._f.read(*args, **kwargs)
                if "large_evidence_bundle.bin" in self._target_name and chunk:
                    self._read_count += 1
                    if self._read_count == 2:
                        fault_injected[0] = True
                        raise OSError("Simulated boundary disk I/O failure on second chunk")
                return chunk

            def __getattr__(self, name):
                return getattr(self._f, name)

        @contextmanager
        def faulty_source(path):
            with real_source(path) as (stream, source_stat):
                if "large_evidence_bundle.bin" in str(path):
                    yield FaultyFileWrapper(stream, str(path)), source_stat
                else:
                    yield stream, source_stat

        delivery_stage_path = stage_dir / DELIVERY_MANIFEST_FILENAME
        delivery = DeliveryManifest.from_dict(
            json.loads(delivery_stage_path.read_text(encoding="utf-8"))
        )

        with patch("ops.a8.e2e.file_safety._regular_source_file", faulty_source):
            graded_root, failure, ledger_failure = export_and_record_delivery(stage_dir, run_dir, delivery)

        self.assertIsNotNone(failure)
        self.assertTrue(fault_injected[0])

        # 1. Verify NO partial file exists under the final destination name
        self.assertFalse(
            large_artifact_dest.exists(),
            msg="A partial/truncated file was left at the final destination path after export failure!",
        )

        # 2. Staging data was never modified
        self.assertEqual(
            sha256_file(large_artifact_stage),
            stage_sha_before,
            msg="Staging file was modified during export failure!",
        )

        # 3. First recovery into the exact same output directory succeeds (exit 0)
        messages_rec1: list[str] = []
        argv = [
            "recover",
            "--run",
            run_id,
            "--output",
            str(self.output),
            "--workspace",
            str(self.workspace),
        ]
        _, args_rec1 = parse_e2e_args(argv)
        exit_code1 = cmd_recover(args_rec1, emit=messages_rec1.append)
        self.assertEqual(
            exit_code1,
            0,
            msg=f"First cmd_recover into same output failed: {messages_rec1}",
        )

        # Destination file now exists and matches staging perfectly
        self.assertTrue(large_artifact_dest.is_file())
        self.assertEqual(sha256_file(large_artifact_dest), stage_sha_before)

        # 4. Second recovery into the exact same output directory is idempotent (exit 0)
        messages_rec2: list[str] = []
        exit_code2 = cmd_recover(args_rec1, emit=messages_rec2.append)
        self.assertEqual(
            exit_code2,
            0,
            msg=f"Second cmd_recover into same output failed: {messages_rec2}",
        )

        # 5. Report passes on recovered destination
        messages_rep: list[str] = []
        _, args_rep = parse_e2e_args(["report", "--run", str(run_dir)])
        exit_rep = cmd_report(args_rep, emit=messages_rep.append)
        self.assertEqual(exit_rep, 0, msg=f"Report failed: {messages_rep}")

    def test_published_bytes_match_the_source_and_preserve_permissions_and_modification_time(self):
        """Export preserves content, permission bits, and modification time."""
        src = self.workspace / "source_valid.txt"
        src.write_text("VERIFIED_INTEGRITY_PAYLOAD", encoding="utf-8")
        src.chmod(0o755)
        os.utime(src, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))

        dst = self.output / "published_valid.txt"
        atomic_publish_file(src, dst)

        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_text(encoding="utf-8"), "VERIFIED_INTEGRITY_PAYLOAD")
        self.assertEqual(sha256_file(dst), sha256_file(src))
        self.assertEqual(stat.S_IMODE(dst.stat().st_mode), 0o755)
        self.assertEqual(dst.stat().st_mtime_ns, src.stat().st_mtime_ns)

    def test_source_replaced_by_private_symlink_before_open_is_not_published(self):
        source = self.workspace / "evidence.json"
        source.write_bytes(b"public evidence")
        private = self.root / "private.json"
        private.write_bytes(b"synthetic private material")
        destination = self.output / "evidence.json"
        real_destination = file_safety._atomic_destination

        @contextmanager
        def swap_before_source_open(*args, **kwargs):
            with real_destination(*args, **kwargs) as stream:
                source.rename(self.workspace / "original-evidence.json")
                source.symlink_to(private)
                yield stream

        with patch("ops.a8.e2e.file_safety._atomic_destination", swap_before_source_open):
            with self.assertRaises(PathSafetyError):
                atomic_publish_file(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(private.read_bytes(), b"synthetic private material")

    def test_source_replaced_by_private_symlink_during_copy_is_not_published(self):
        source = self.workspace / "evidence.json"
        source.write_bytes(b"public evidence")
        private = self.root / "private.json"
        private.write_bytes(b"synthetic private material")
        destination = self.output / "evidence.json"
        real_source = file_safety._regular_source_file
        original = self.workspace / "original-evidence.json"

        @contextmanager
        def replace_after_first_read(path):
            with real_source(path) as (stream, source_stat):
                class ReplacingReader:
                    replaced = False

                    def read(self, *args, **kwargs):
                        content = stream.read(*args, **kwargs)
                        if content and not self.replaced:
                            source.rename(original)
                            source.symlink_to(private)
                            self.replaced = True
                        return content

                    def __getattr__(self, name):
                        return getattr(stream, name)

                yield ReplacingReader(), source_stat

        with patch("ops.a8.e2e.file_safety._regular_source_file", replace_after_first_read):
            with self.assertRaises(PathSafetyError):
                atomic_publish_file(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(private.read_bytes(), b"synthetic private material")

    def test_source_changes_between_copy_and_verification_preserve_destination_and_remove_temporary_files(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                source = self.workspace / f"source-{replace}"
                destination = self.output / f"destination-{replace}"
                original = b"ORIGINAL SOURCE"
                changed = b"CHANGED SOURCE"
                source.write_bytes(original)
                if replace:
                    destination.write_bytes(b"EXISTING DESTINATION")
                before = {p.name: p.read_bytes() for p in self.output.iterdir()}
                real_source = file_safety._regular_source_file
                changed_once = []

                @contextmanager
                def change_after_first_read(path):
                    with real_source(path) as (stream, source_stat):
                        class MutatingReader:
                            def read(self, *args, **kwargs):
                                content = stream.read(*args, **kwargs)
                                if content and not changed_once:
                                    # Change the same inode after the copied bytes
                                    # were read; the second hash must observe it.
                                    source.write_bytes(changed)
                                    changed_once.append(True)
                                return content

                            def __getattr__(self, name):
                                return getattr(stream, name)

                        yield MutatingReader(), source_stat

                with patch("ops.a8.e2e.file_safety._regular_source_file", change_after_first_read):
                    with self.assertRaises(IntegrityError) as failure:
                        atomic_publish_file(source, destination, replace=replace)
                self.assertTrue(changed_once)
                self.assertEqual(failure.exception.details["actual"], hashlib.sha256(original).hexdigest())
                self.assertEqual(failure.exception.details["expected"], hashlib.sha256(changed).hexdigest())
                self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)

    def test_a_missing_source_closes_the_temporary_descriptor_and_preserves_the_destination(self):
        source = self.workspace / "source"
        source.write_bytes(b"NEW")
        destination = self.output / "destination"
        destination.write_bytes(b"OLD")
        # Derive the failing case from a valid copy by removing only its source.
        source.unlink()
        opened = []
        real_open = os.open

        def track_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                opened.append(fd)
            return fd

        with patch("ops.a8.e2e.file_safety.os.open", side_effect=track_open):
            with self.assertRaises(FileNotFoundError):
                atomic_publish_file(source, destination)
        self.assertEqual(len(opened), 1)
        try:
            with self.assertRaises(OSError) as failure:
                os.fstat(opened[0])
            self.assertEqual(failure.exception.errno, errno.EBADF)
        finally:
            # Avoid leaking in the test process if a regression leaves it open.
            try:
                os.close(opened[0])
            except OSError:
                pass
        self.assertEqual(destination.read_bytes(), b"OLD")
        self.assertEqual(list(self.output.iterdir()), [destination])

    def test_a_parent_replaced_during_sync_is_rejected_and_cleanup_stays_in_the_original_directory(self):
        source = self.workspace / "source"
        source.write_bytes(b"NEW")
        real_fsync = os.fsync
        for operation in ("write", "copy"):
            for symlink in (False, True):
                with self.subTest(operation=operation, symlink=symlink):
                    parent = self.output / f"{operation}-{symlink}"
                    parent.mkdir()
                    moved = self.output / f"{operation}-{symlink}-moved"
                    destination = parent / "destination"
                    destination.write_bytes(b"OLD")
                    substitute = self.outside / f"{operation}-{symlink}"
                    substitute.mkdir()

                    def swap_parent(fd):
                        real_fsync(fd)
                        candidate = next(parent.glob(".destination.tmp.*")).name
                        parent.rename(moved)
                        if symlink:
                            parent.symlink_to(substitute, target_is_directory=True)
                        else:
                            parent.mkdir()
                        (parent / candidate).write_bytes(b"SUBSTITUTE")

                    with patch("ops.a8.e2e.file_safety.os.fsync", side_effect=swap_parent):
                        with self.assertRaises(PathSafetyError):
                            if operation == "write":
                                atomic_write_bytes(destination, b"NEW", package_root=self.output)
                            else:
                                atomic_publish_file(source, destination, package_root=self.output)
                    self.assertEqual((moved / "destination").read_bytes(), b"OLD")
                    self.assertEqual(list(moved.iterdir()), [moved / "destination"])
                    self.assertFalse(destination.exists())
                    planted = list(parent.iterdir())
                    self.assertEqual(len(planted), 1)
                    self.assertEqual(planted[0].read_bytes(), b"SUBSTITUTE")

    def test_a_parent_swapped_at_rename_cannot_redirect_publication_to_the_symlink_target(self):
        destination = self.output / "destination"
        destination.write_bytes(b"OLD")
        moved = self.root / "original-output"
        real_replace = os.replace

        def swap_at_replace(src, dst, **kwargs):
            # This boundary is after the final checks: only anchored operations
            # can prevent a late substitution from redirecting publication.
            self.output.rename(moved)
            self.output.symlink_to(self.outside, target_is_directory=True)
            (self.outside / Path(src).name).write_bytes(b"SUBSTITUTE")
            return real_replace(src, dst, **kwargs)

        with patch("ops.a8.e2e.file_safety.os.replace", side_effect=swap_at_replace):
            atomic_write_bytes(destination, b"NEW", package_root=self.output)
        self.assertEqual((moved / "destination").read_bytes(), b"NEW")
        self.assertEqual(list(moved.iterdir()), [moved / "destination"])
        self.assertFalse((self.outside / "destination").exists())
        self.assertEqual(next(self.outside.iterdir()).read_bytes(), b"SUBSTITUTE")

    def test_cleanup_after_a_failed_rename_does_not_unlink_a_file_in_a_substituted_parent(self):
        destination = self.output / "destination"
        destination.write_bytes(b"OLD")
        moved = self.root / "original-output"

        def fail_after_swap(src, dst, **kwargs):
            self.output.rename(moved)
            self.output.symlink_to(self.outside, target_is_directory=True)
            (self.outside / Path(src).name).write_bytes(b"SUBSTITUTE")
            raise OSError("Synthetic rename failure")

        with patch("ops.a8.e2e.file_safety.os.replace", side_effect=fail_after_swap):
            with self.assertRaises(OSError):
                atomic_write_bytes(destination, b"NEW")
        self.assertEqual((moved / "destination").read_bytes(), b"OLD")
        self.assertEqual(list(moved.iterdir()), [moved / "destination"])
        self.assertEqual(next(self.outside.iterdir()).read_bytes(), b"SUBSTITUTE")

    def test_a_parent_swapped_at_hard_link_publication_cannot_redirect_export_or_cleanup(self):
        source = self.workspace / "source"
        source.write_bytes(b"NEW")
        destination = self.output / "destination"
        moved = self.root / "original-output"
        real_link = os.link
        injected = []

        def swap_at_link(src, dst, **kwargs):
            self.output.rename(moved)
            self.output.symlink_to(self.outside, target_is_directory=True)
            substitute = self.outside / Path(src).name
            substitute.write_bytes(b"SUBSTITUTE")
            injected.append(substitute)
            return real_link(src, dst, **kwargs)

        with patch("ops.a8.e2e.file_safety.os.link", side_effect=swap_at_link):
            atomic_publish_file(source, destination, package_root=self.output, replace=False)
        self.assertEqual(len(injected), 1)
        self.assertEqual((moved / "destination").read_bytes(), b"NEW")
        self.assertEqual(list(moved.iterdir()), [moved / "destination"])
        self.assertEqual(list(self.outside.iterdir()), injected)
        self.assertEqual(injected[0].read_bytes(), b"SUBSTITUTE")
        self.assertEqual(source.read_bytes(), b"NEW")

    def test_a_failed_hard_link_after_parent_substitution_cleans_only_the_original_temporary_file(self):
        source = self.workspace / "source"
        source.write_bytes(b"NEW")
        destination = self.output / "destination"
        moved = self.root / "original-output"
        injected = []
        publication_error = OSError("Synthetic hard-link failure")

        def fail_after_swap(src, dst, **kwargs):
            self.output.rename(moved)
            self.output.symlink_to(self.outside, target_is_directory=True)
            substitute = self.outside / Path(src).name
            substitute.write_bytes(b"SUBSTITUTE")
            injected.append(substitute)
            raise publication_error

        with patch("ops.a8.e2e.file_safety.os.link", side_effect=fail_after_swap):
            with self.assertRaises(OSError) as failure:
                atomic_publish_file(source, destination, replace=False)
        self.assertIs(failure.exception, publication_error)
        self.assertEqual(len(injected), 1)
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual(list(self.outside.iterdir()), injected)
        self.assertEqual(injected[0].read_bytes(), b"SUBSTITUTE")
        self.assertEqual(source.read_bytes(), b"NEW")

    def test_a_parent_replaced_before_it_is_opened_cannot_create_directories_outside_the_package(self):
        parent = self.output / "parent"
        parent.mkdir()
        destination = parent / "new-directory" / "destination"
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "parent" and not swapped:
                swapped = True
                parent.rmdir()
                parent.symlink_to(self.outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch("ops.a8.e2e.file_safety.os.open", side_effect=swap_before_open):
            with self.assertRaises(PathSafetyError):
                atomic_write_bytes(destination, b"NEW", package_root=self.output)
        self.assertTrue(swapped)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_substituted_temporary_inode_is_neither_published_nor_removed(self):
        destination = self.output / "destination"
        destination.write_bytes(b"OLD")
        real_fsync = os.fsync

        def substitute_temporary(fd):
            real_fsync(fd)
            candidate = next(self.output.glob(".destination.tmp.*"))
            candidate.unlink()
            candidate.write_bytes(b"SUBSTITUTE")

        with patch("ops.a8.e2e.file_safety.os.fsync", side_effect=substitute_temporary):
            with self.assertRaises(PathSafetyError):
                atomic_write_bytes(destination, b"NEW")
        self.assertEqual(destination.read_bytes(), b"OLD")
        self.assertEqual(next(self.output.glob(".destination.tmp.*")).read_bytes(), b"SUBSTITUTE")

    def test_atomic_write_bytes_refuses_a_symlink_destination_and_preserves_its_target(self):
        """A pre-existing destination symlink is refused before writing its target."""
        sentinel = self.outside / "sym_target.txt"
        sentinel.write_text("ORIGINAL", encoding="utf-8")

        link = self.output / "link.txt"
        link.symlink_to(sentinel)

        with self.assertRaises(PathSafetyError):
            atomic_write_bytes(link, b"NEW_DATA")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "ORIGINAL")

    def test_manifest_with_traversal_suite_relpath_rejected_without_external_writes(self):
        pkg_dir = self.root / "pkg_traversal"
        setup_baseline_package(pkg_dir, scenarios=["wasm-abi-boundary"], suite_relpath="suite")
        exec_file = pkg_dir / EXECUTION_MANIFEST_FILENAME
        m_data = json.loads(exec_file.read_text(encoding="utf-8"))
        m_data["suite_export_relpath"] = "../outside"
        exec_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")

        outside_canary = self.outside / "canary.txt"
        outside_canary.write_text("original-canary-content\n", encoding="utf-8")

        outcome = grade_run_package(pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertTrue(
            any(f.code == "SUITE_PATH_UNSAFE" for f in outcome.findings),
            f"Expected SUITE_PATH_UNSAFE in findings: {outcome.findings}",
        )

        _, args = parse_e2e_args(["report", "--run", str(pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

        self.assertEqual(outside_canary.read_text(encoding="utf-8"), "original-canary-content\n")

    def test_manifest_with_absolute_suite_relpath_rejected_without_external_writes(self):
        pkg_dir = self.root / "pkg_absolute"
        setup_baseline_package(pkg_dir, scenarios=["wasm-abi-boundary"], suite_relpath="suite")
        exec_file = pkg_dir / EXECUTION_MANIFEST_FILENAME
        m_data = json.loads(exec_file.read_text(encoding="utf-8"))
        m_data["suite_export_relpath"] = str(self.outside)
        exec_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")

        outside_canary = self.outside / "canary_abs.txt"
        outside_canary.write_text("original-canary-abs\n", encoding="utf-8")

        outcome = grade_run_package(pkg_dir)
        self.assertEqual(outcome.exit_code, 1)
        self.assertTrue(
            any(f.code == "SUITE_PATH_UNSAFE" for f in outcome.findings),
            f"Expected SUITE_PATH_UNSAFE in findings: {outcome.findings}",
        )

        _, args = parse_e2e_args(["report", "--run", str(pkg_dir)])
        report_code = cmd_report(args, emit=lambda m: None)
        self.assertEqual(report_code, 1)

        self.assertEqual(outside_canary.read_text(encoding="utf-8"), "original-canary-abs\n")

    def test_export_rejects_symlink_in_destination_root(self):
        stage = self.root / "stage"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "file.txt").write_text("hello\n", encoding="utf-8")

        dest_symlink = self.root / "dest_link"
        dest_symlink.symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises((ExportConflict, PathSafetyError)):
            export_run_package(stage, dest_symlink)

    def test_export_rejects_intermediate_directory_symlink(self):
        stage = self.root / "stage_inter"
        (stage / "nested").mkdir(parents=True, exist_ok=True)
        (stage / "nested" / "file.txt").write_text("hello\n", encoding="utf-8")

        dest = self.root / "dest_inter"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "nested").symlink_to(self.outside, target_is_directory=True)

        with self.assertRaises((ExportConflict, PathSafetyError)):
            export_run_package(stage, dest)

    def test_export_rejects_dangling_final_symlink(self):
        stage = self.root / "stage_dangling"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "file.txt").write_text("hello\n", encoding="utf-8")

        dest = self.root / "dest_dangling"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "file.txt").symlink_to(self.root / "nonexistent_target")

        with self.assertRaises((ExportConflict, PathSafetyError)):
            export_run_package(stage, dest)

        self.assertFalse((self.root / "nonexistent_target").exists())

    def test_export_rejects_source_root_symlink(self):
        real_stage = self.root / "real_stage"
        real_stage.mkdir(parents=True, exist_ok=True)
        (real_stage / "file.txt").write_text("data\n", encoding="utf-8")

        sym_stage = self.root / "sym_stage"
        sym_stage.symlink_to(real_stage, target_is_directory=True)

        dest = self.root / "dest_source_sym"
        dest.mkdir(parents=True, exist_ok=True)

        with self.assertRaises((ExportConflict, PathSafetyError)):
            export_run_package(sym_stage, dest)

    def test_positive_matching_reexport_succeeds_cleanly(self):
        stage = self.root / "stage_pos"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "file.txt").write_text("hello\n", encoding="utf-8")
        (stage / "sub").mkdir()
        (stage / "sub" / "data.json").write_text('{"a": 1}\n', encoding="utf-8")

        dest = self.root / "dest_pos"
        export_run_package(stage, dest)
        self.assertTrue((dest / "file.txt").is_file())
        self.assertTrue((dest / "sub" / "data.json").is_file())

        export_run_package(stage, dest)
        self.assertEqual((dest / "file.txt").read_text(encoding="utf-8"), "hello\n")
        self.assertEqual((dest / "sub" / "data.json").read_text(encoding="utf-8"), '{"a": 1}\n')

    def test_reexport_rejects_existing_file_replaced_by_symlink_after_path_check(self):
        stage = self.root / "stage-existing-race"
        stage.mkdir()
        (stage / "evidence.json").write_bytes(b"synthetic evidence")
        destination = self.root / "destination-existing-race"
        destination.mkdir()
        existing = destination / "evidence.json"
        existing.write_bytes(b"synthetic evidence")
        outside = self.root / "outside-evidence.json"
        outside.write_bytes(b"synthetic evidence")
        real_assert = runpackage.assert_no_symlinks_in_path
        substituted = False

        def swap_after_check(path, *, what):
            nonlocal substituted
            result = real_assert(path, what=what)
            if Path(path) == existing and not substituted:
                substituted = True
                existing.rename(self.root / "original-existing-evidence.json")
                existing.symlink_to(outside)
            return result

        with patch("ops.a8.e2e.runpackage.assert_no_symlinks_in_path", side_effect=swap_after_check):
            with self.assertRaises(PathSafetyError):
                export_run_package(stage, destination)
        self.assertTrue(substituted)
        self.assertEqual(outside.read_bytes(), b"synthetic evidence")

    def test_export_cannot_create_nested_directory_through_parent_swapped_after_check(self):
        stage = self.root / "stage-directory-race"
        (stage / "nested" / "deep").mkdir(parents=True)
        (stage / "nested" / "deep" / "file.json").write_bytes(b"synthetic evidence")
        destination = self.root / "destination-directory-race"
        (destination / "nested").mkdir(parents=True)
        outside = self.root / "outside-directory-race"
        outside.mkdir()
        real_assert = runpackage.assert_no_symlinks_in_path
        checked = destination / "nested" / "deep"
        substituted = False

        def swap_after_check(path, *, what):
            nonlocal substituted
            result = real_assert(path, what=what)
            if Path(path) == checked and not substituted:
                substituted = True
                (destination / "nested").rename(self.root / "moved-nested")
                (destination / "nested").symlink_to(outside, target_is_directory=True)
            return result

        with patch("ops.a8.e2e.runpackage.assert_no_symlinks_in_path", side_effect=swap_after_check):
            with self.assertRaises(PathSafetyError):
                export_run_package(stage, destination)
        self.assertTrue(substituted)
        self.assertFalse((outside / "deep").exists())


if __name__ == "__main__":
    unittest.main()
