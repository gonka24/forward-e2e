"""Check collector path filtering, byte preservation, and index updates.

Payloads are opaque synthetic bytes: the collector copies but does not parse them.
All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
import copy
from dataclasses import replace
import os
import shutil
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.a8.collector import (
    ArtifactIndexError,
    CollectorSecurityError,
    collect_snapshot_artifacts,
    copy_checked_runtime_file,
    copy_runtime_artifact,
    is_path_safe,
    sha256_checked_artifact,
    update_artifact_index,
)
from ops.a8.runtime import RuntimeSnapshot, sha256_file


class CollectorTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-collector-")
        self.addCleanup(tmp_dir.cleanup)
        self.root = Path(tmp_dir.name)
        self.runtime_root = self.root / "runtime"
        self.suite_dir = self.root / "suite-export"
        self.suite_dir.mkdir()

    def make_snapshot(self, run_id="collect-run-01"):
        snapshot = RuntimeSnapshot(self.runtime_root, run_id)
        snapshot.evidence_dir.mkdir(parents=True)
        (snapshot.evidence_dir / "live-context.json").write_bytes(b"{}\n")
        return snapshot

    def test_normal_paths_are_safe_but_traversal_and_denied_segments_are_rejected(self):
        snap = self.make_snapshot()
        self.assertTrue(is_path_safe(snap.run_dir, snap.evidence_dir / "live-context.json"))
        outside = snap.run_dir.parent / "secret.txt"
        outside.write_bytes(b"synthetic secret")
        self.assertFalse(is_path_safe(snap.run_dir, snap.run_dir / ".." / outside.name))

        for denied in ("node_key", "priv_validator_key", "keyring-test", "keyring",
                       "id_rsa", "mnemonic", ".git", "target", ".gradle", "prod-local"):
            with self.subTest(denied=denied):
                path = snap.evidence_dir / denied / "live-context.json"
                path.parent.mkdir()
                path.write_bytes(b"{}\n")
                self.assertFalse(is_path_safe(snap.run_dir, path))

    def test_only_allowlisted_files_are_copied_with_original_bytes_and_hashes(self):
        snap = self.make_snapshot()
        snap.junit_dir.mkdir()
        junit = snap.junit_dir / "TEST-MarketplaceContractAcceptanceTests.xml"
        junit.write_bytes(b"<testsuite/>\n")
        for name in ("priv_validator_key.json", "mnemonic.txt", "unlisted.txt"):
            (snap.run_dir / name).write_bytes(b"must not be exported")

        entries, errors = collect_snapshot_artifacts(snap, "lock-exact-e", self.suite_dir)
        self.assertEqual(errors, [])
        expected = {
            f"runs/{snap.run_id}/{source.relative_to(snap.run_dir).as_posix()}": source
            for source in (snap.evidence_dir / "live-context.json", junit)
        }
        self.assertCountEqual([entry.relative_path for entry in entries], expected)
        self.assertCountEqual(
            [path.relative_to(self.suite_dir).as_posix()
             for path in self.suite_dir.rglob("*") if path.is_file()],
            expected,
        )
        for entry in entries:
            with self.subTest(path=entry.relative_path):
                source = expected[entry.relative_path]
                dest = self.suite_dir / entry.relative_path
                self.assertEqual(dest.read_bytes(), source.read_bytes())
                self.assertEqual(entry.sha256, sha256_file(source))
                self.assertEqual(entry.size_bytes, source.stat().st_size)
                self.assertEqual(entry.run_id, snap.run_id)
                self.assertEqual(entry.task_id, "lock-exact-e")

    def test_an_allowlisted_symlink_escaping_the_snapshot_is_rejected_without_copying(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        outside = self.root / "outside.json"
        source.rename(outside)
        # Only the source's location changes; its allowlisted name and bytes remain.
        source.symlink_to(outside)

        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Security: rejected", errors[0])
        self.assertFalse((self.suite_dir / "runs" / snap.run_id / "evidence" / source.name).exists())

    def test_an_allowlisted_file_under_a_denied_segment_is_rejected_without_copying(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        denied = snap.evidence_dir / "keyring-test" / source.name
        denied.parent.mkdir()
        # Both layouts match the allowlist; only the denied segment makes this unsafe.
        source.rename(denied)

        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Security: rejected", errors[0])
        self.assertFalse((self.suite_dir / "runs" / snap.run_id / denied.relative_to(snap.run_dir)).exists())

    def test_allowlisted_directories_do_not_export_key_files_with_extensions(self):
        snap = self.make_snapshot()
        secrets = (
            snap.evidence_dir / "genesis" / "priv_validator_key.json",
            snap.evidence_dir / "external-harness" / "node_key.json",
            snap.evidence_dir / "external-harness" / "mnemonic.txt",
        )
        for secret in secrets:
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_bytes(b"synthetic private material")

        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)

        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)  # the ordinary live-context.json
        for secret in secrets:
            relative = secret.relative_to(snap.run_dir)
            self.assertFalse(is_path_safe(snap.run_dir, secret))
            self.assertFalse((self.suite_dir / "runs" / snap.run_id / relative).exists())
            self.assertFalse(any(entry.relative_path.endswith(relative.as_posix()) for entry in entries))

    def test_declared_genesis_and_external_harness_evidence_is_exported(self):
        snap = self.make_snapshot()
        names = {
            "genesis": (
                "genesis-before-b3.json", "genesis-after-b3.json", "genesis-final.json",
                "b3-provision.json", "b3-genesis-verification.json",
            ),
            "external-harness": (
                "api-compat.json", "upstream-classpath.log", "testermint-classpath.txt",
                "testermint-classpath.txt.json", "harness-inputs.json", "harness-build.log",
            ),
        }
        expected = set()
        for directory, filenames in names.items():
            folder = snap.evidence_dir / directory
            folder.mkdir()
            for filename in filenames:
                source = folder / filename
                source.write_bytes(b"synthetic evidence")
                expected.add(f"runs/{snap.run_id}/evidence/{directory}/{filename}")

        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        expected.add(f"runs/{snap.run_id}/evidence/live-context.json")
        self.assertEqual({entry.relative_path for entry in entries}, expected)

    def test_a_corrupted_copy_reports_integrity_failure_and_is_not_indexed(self):
        snap = self.make_snapshot()

        def corrupt_copy(source, destination):
            content = bytearray(source.read())
            # Equal lengths ensure a size-only integrity check cannot pass this test.
            content[0] ^= 1
            destination.write(content)

        # Simulate corruption at the filesystem boundary, keeping the collector real.
        with patch("ops.a8.collector.shutil.copyfileobj", side_effect=corrupt_copy) as copy:
            entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        copy.assert_called_once()
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Integrity mismatch after copy: evidence/live-context.json", errors[0])

    @unittest.skipUnless(os.name == "posix", "descriptor-relative source opening requires POSIX")
    def test_source_replaced_by_secret_symlink_after_path_check_is_not_exported(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        outside = self.root / "private.json"
        outside.write_bytes(b"synthetic private material")
        real_guard = is_path_safe
        replaced = False

        def swap_after_check(base_dir, target_path):
            nonlocal replaced
            safe = real_guard(base_dir, target_path)
            if target_path == source and safe and not replaced:
                replaced = True
                source.unlink()
                source.symlink_to(outside)
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_after_check):
            entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertTrue(replaced)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("rejected changed or unsafe source", errors[0])
        self.assertFalse((self.suite_dir / "runs" / snap.run_id / "evidence/live-context.json").exists())

    @unittest.skipUnless(os.name == "posix", "descriptor-relative hashing requires POSIX")
    def test_hash_rejects_source_replaced_by_private_symlink_after_path_check(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        private = self.root / "private.json"
        private.write_bytes(b"synthetic private material")
        real_guard = is_path_safe
        substituted = False

        def swap_after_check(base_dir, target_path):
            nonlocal substituted
            safe = real_guard(base_dir, target_path)
            if target_path == source and safe and not substituted:
                substituted = True
                source.rename(self.root / "original-context.json")
                source.symlink_to(private)
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_after_check):
            with self.assertRaises(CollectorSecurityError):
                sha256_checked_artifact(snap.run_dir, source)
        self.assertTrue(substituted)
        self.assertEqual(private.read_bytes(), b"synthetic private material")

    @unittest.skipUnless(os.name == "posix", "descriptor-relative publication requires POSIX")
    def test_destination_parent_replaced_by_symlink_after_path_check_cannot_export_outside_suite(self):
        snap = self.make_snapshot()
        parent = self.suite_dir / "runs" / snap.run_id / "evidence"
        parent.mkdir(parents=True)
        moved = self.root / "moved-evidence"
        outside = self.root / "outside"
        outside.mkdir()
        destination = parent / "live-context.json"
        real_guard = is_path_safe
        replaced = False

        def swap_after_check(base_dir, target_path):
            nonlocal replaced
            safe = real_guard(base_dir, target_path)
            if target_path == destination and safe and not replaced:
                replaced = True
                parent.rename(moved)
                parent.symlink_to(outside, target_is_directory=True)
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_after_check):
            entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertTrue(replaced)
        self.assertEqual(entries, [])
        self.assertTrue(errors)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "descriptor-relative publication requires POSIX")
    def test_destination_parent_replaced_during_copy_cannot_publish_outside_suite(self):
        snap = self.make_snapshot()
        parent = self.suite_dir / "runs" / snap.run_id / "evidence"
        parent.mkdir(parents=True)
        moved = self.root / "moved-evidence"
        outside = self.root / "outside"
        outside.mkdir()
        real_copy = shutil.copyfileobj

        def swap_after_copy(source, destination):
            real_copy(source, destination)
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)

        with patch("ops.a8.collector.shutil.copyfileobj", side_effect=swap_after_copy):
            entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("rejected changed destination", errors[0])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_index_updates_preserve_old_and_new_entries_without_duplicates_or_self_hash(self):
        expected = {}
        # Reverse insertion order makes the sorted-output assertion meaningful.
        for run_id in ("index-run-02", "index-run-01"):
            snap = self.make_snapshot(run_id)
            entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
            self.assertEqual(errors, [])
            self.assertEqual(len(entries), 1)
            expected.update({entry.relative_path: entry.to_dict() for entry in entries})
            # Repeat to verify updates are idempotent even when an index already exists.
            for attempt in range(2):
                with self.subTest(run_id=run_id, attempt=attempt):
                    index = update_artifact_index(self.suite_dir, entries)
                    self.assertEqual(index, self.suite_dir / "artifact-index.json")
                    data = json.loads(index.read_text(encoding="utf-8"))
                    self.assertEqual(data["schema_version"], "1.0.0")
                    self.assertEqual(data["total_artifacts"], len(expected))
                    # Exact equality also excludes any self-entry or unexpected artifact.
                    self.assertEqual(data["artifacts"], [expected[key] for key in sorted(expected)])

    def test_recollecting_a_changed_file_replaces_its_index_record_without_duplicates(self):
        snap = self.make_snapshot()
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        update_artifact_index(self.suite_dir, entries)
        original = entries[0]

        (snap.evidence_dir / "live-context.json").write_bytes(b'{"updated": true}\n')
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].relative_path, original.relative_path)
        self.assertNotEqual(entries[0].sha256, original.sha256)
        index = update_artifact_index(self.suite_dir, entries)
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(data["total_artifacts"], 1)
        self.assertEqual(data["artifacts"], [entries[0].to_dict()])

    def test_nested_task_evidence_is_copied_under_its_original_run_id_path(self):
        snap = self.make_snapshot("collect-run-02")
        source = snap.evidence_dir / "live-context.json"
        # The live harness uses evidence/<run-id>/ rather than the flat layout.
        task_evidence = snap.evidence_dir / snap.run_id
        task_evidence.mkdir()
        source.rename(task_evidence / source.name)

        entries, errors = collect_snapshot_artifacts(snap, "lock-exact-e", self.suite_dir)
        relative = f"runs/{snap.run_id}/evidence/{snap.run_id}/live-context.json"
        self.assertEqual(errors, [])
        self.assertEqual([entry.relative_path for entry in entries], [relative])
        self.assertEqual((self.suite_dir / relative).read_bytes(), (task_evidence / source.name).read_bytes())

    def test_an_internal_symlink_cannot_export_git_configuration(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        secret = snap.run_dir / ".git" / "config"
        secret.parent.mkdir()
        source.rename(secret)
        source.symlink_to(secret)
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertTrue(errors)

    def test_an_intermediate_source_symlink_is_rejected_even_inside_the_snapshot(self):
        snap = self.make_snapshot()
        original = snap.evidence_dir
        relocated = snap.run_dir / "relocated"
        original.rename(relocated)
        original.symlink_to(relocated, target_is_directory=True)
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertTrue(errors)

    def test_destination_symlinks_never_overwrite_files_outside_the_suite(self):
        snap = self.make_snapshot()
        outside = self.root / "outside"
        outside.mkdir()
        protected = outside / "live-context.json"
        protected.write_bytes(b"original")
        for component in ("runs", f"runs/{snap.run_id}",
                          f"runs/{snap.run_id}/evidence",
                          f"runs/{snap.run_id}/evidence/live-context.json"):
            with self.subTest(component=component), tempfile.TemporaryDirectory(dir=self.root) as folder:
                suite = Path(folder)
                link = suite / component
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(protected if link.name.endswith(".json") else outside)
                entries, errors = collect_snapshot_artifacts(snap, "test-task", suite)
                self.assertEqual(entries, [])
                self.assertTrue(errors)
                self.assertEqual(protected.read_bytes(), b"original")
                self.assertEqual(list(outside.iterdir()), [protected])

    def test_a_directory_destination_cannot_redirect_the_copy_to_an_external_file(self):
        snap = self.make_snapshot()
        destination = self.suite_dir / "runs" / snap.run_id / "evidence/live-context.json"
        destination.mkdir(parents=True)
        outside = self.root / "protected.json"
        outside.write_bytes(b"original")
        (destination / "live-context.json").symlink_to(outside)
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("non-file destination", errors[0])
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertEqual(list(destination.iterdir()), [destination / "live-context.json"])

    def test_replacing_a_hard_link_does_not_overwrite_its_external_inode(self):
        snap = self.make_snapshot()
        destination = self.suite_dir / "runs" / snap.run_id / "evidence/live-context.json"
        destination.parent.mkdir(parents=True)
        outside = self.root / "protected.json"
        outside.write_bytes(b"original")
        os.link(outside, destination)
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(outside.read_bytes(), b"original")
        self.assertEqual(destination.read_bytes(), (snap.evidence_dir / "live-context.json").read_bytes())
        self.assertFalse(destination.samefile(outside))

    @unittest.skipUnless(os.name == "posix", "exclusive recovery publication requires POSIX")
    def test_recovery_copy_does_not_overwrite_destination_created_during_copy(self):
        snap = self.make_snapshot()
        source = snap.evidence_dir / "live-context.json"
        destination = self.suite_dir / "runs" / snap.run_id / "evidence/live-context.json"
        real_copy = shutil.copyfileobj

        def create_conflicting_destination(source_stream, destination_stream):
            real_copy(source_stream, destination_stream)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"previous evidence")

        with patch("ops.a8.collector.shutil.copyfileobj", side_effect=create_conflicting_destination):
            with self.assertRaises(CollectorSecurityError):
                copy_runtime_artifact(snap.run_dir, source, self.suite_dir, snap.run_id, "test-task")
        self.assertEqual(destination.read_bytes(), b"previous evidence")
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_failed_artifact_replacement_preserves_the_original_and_removes_the_temporary_copy(self):
        snap = self.make_snapshot()
        _, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        destination = self.suite_dir / "runs" / snap.run_id / "evidence/live-context.json"
        original = destination.read_bytes()
        (snap.evidence_dir / "live-context.json").write_bytes(b"new evidence")
        with patch("ops.a8.collector.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_new_indexes_are_readable_and_updates_preserve_existing_permissions(self):
        index = update_artifact_index(self.suite_dir, [])
        self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o644)
        for mode in (0o644, 0o640, 0o600):
            with self.subTest(mode=oct(mode)):
                index.chmod(mode)
                update_artifact_index(self.suite_dir, [])
                self.assertEqual(stat.S_IMODE(index.stat().st_mode), mode)

    def test_a_corrupt_existing_index_is_preserved_and_reported(self):
        index = update_artifact_index(self.suite_dir, [])
        valid = index.read_bytes()
        malformed_shape = json.loads(valid)
        malformed_shape["artifacts"] = None
        for malformed in (valid[:-2], json.dumps(malformed_shape).encode()):
            with self.subTest(malformed=malformed):
                index.write_bytes(malformed)
                with self.assertRaises(ArtifactIndexError):
                    update_artifact_index(self.suite_dir, [])
                self.assertEqual(index.read_bytes(), malformed)

    def test_missing_or_invalid_index_facts_are_rejected_without_rewriting_evidence(self):
        snap = self.make_snapshot()
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        index = update_artifact_index(self.suite_dir, entries)
        positive = json.loads(index.read_text())
        mutations = [("envelope", field, None) for field in ("schema_version", "total_artifacts")]
        mutations += [("artifact", field, None) for field in positive["artifacts"][0]]
        mutations += [
            ("envelope", "schema_version", "9.0.0"),
            ("envelope", "total_artifacts", 2),
            ("envelope", "total_artifacts", True),
            ("artifact", "sha256", "not-a-hash"),
            ("artifact", "size_bytes", -1),
            ("artifact", "size_bytes", True),
            ("artifact", "relative_path", "../outside.json"),
        ]
        for scope, field, value in mutations:
            with self.subTest(scope=scope, field=field, value=value):
                negative = copy.deepcopy(positive)
                target = negative if scope == "envelope" else negative["artifacts"][0]
                if value is None:
                    del target[field]
                else:
                    target[field] = value
                original = json.dumps(negative).encode()
                index.write_bytes(original)
                with self.assertRaises(ArtifactIndexError):
                    update_artifact_index(self.suite_dir, [])
                self.assertEqual(index.read_bytes(), original)

    def test_invalid_new_index_entries_cannot_replace_a_valid_index(self):
        snap = self.make_snapshot()
        entries, errors = collect_snapshot_artifacts(snap, "test-task", self.suite_dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        index = update_artifact_index(self.suite_dir, entries)
        original = index.read_bytes()
        entry = entries[0]
        invalid = (
            replace(entry, relative_path="../outside.json"),
            replace(entry, relative_path=r"C:\outside.json"),
            replace(entry, relative_path=r"runs\outside.json"),
            replace(entry, sha256="not-a-sha"),
            replace(entry, size_bytes=-1),
        )
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ArtifactIndexError):
                update_artifact_index(self.suite_dir, [bad])
            self.assertEqual(index.read_bytes(), original)
        with self.assertRaises(ArtifactIndexError):
            update_artifact_index(self.suite_dir, [entry, entry])
        self.assertEqual(index.read_bytes(), original)

    def test_failed_atomic_index_replacement_preserves_the_old_index_and_removes_temporary_file(self):
        index = update_artifact_index(self.suite_dir, [])
        old = index.read_bytes()
        with patch("ops.a8.collector.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                update_artifact_index(self.suite_dir, [])
        self.assertEqual(index.read_bytes(), old)
        self.assertEqual(list(self.suite_dir.iterdir()), [index])

    def test_an_index_symlink_is_rejected_without_changing_its_target(self):
        index = update_artifact_index(self.suite_dir, [])
        target = self.root / "outside-index.json"
        index.rename(target)
        old = target.read_bytes()
        index.symlink_to(target)
        with self.assertRaises(CollectorSecurityError):
            update_artifact_index(self.suite_dir, [])
        self.assertEqual(target.read_bytes(), old)

    @unittest.skipUnless(os.name == "posix", "descriptor-relative index publication requires POSIX")
    def test_suite_root_replaced_after_index_path_check_cannot_write_index_outside_suite(self):
        outside = self.root / "outside"
        outside.mkdir()
        moved = self.root / "moved-suite"
        index = self.suite_dir / "artifact-index.json"
        real_guard = is_path_safe
        replaced = False

        def swap_after_check(base_dir, target_path):
            nonlocal replaced
            safe = real_guard(base_dir, target_path)
            if target_path == index and safe and not replaced:
                replaced = True
                self.suite_dir.rename(moved)
                self.suite_dir.symlink_to(outside, target_is_directory=True)
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_after_check):
            with self.assertRaises(CollectorSecurityError):
                update_artifact_index(self.suite_dir, [])
        self.assertTrue(replaced)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "ancestor symlink race requires POSIX")
    def test_suite_parent_replaced_after_path_check_cannot_write_index_outside_suite(self):
        bridge = self.root / "bridge"
        suite = bridge / "suite"
        suite.mkdir(parents=True)
        outside = self.root / "outside-parent"
        (outside / "suite").mkdir(parents=True)
        moved = self.root / "original-bridge"
        real_guard = is_path_safe
        swapped = False

        def swap_parent_after_check(base_dir, target_path):
            nonlocal swapped
            safe = real_guard(base_dir, target_path)
            if target_path == suite / "artifact-index.json" and safe and not swapped:
                bridge.rename(moved)
                bridge.symlink_to(outside, target_is_directory=True)
                swapped = True
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_parent_after_check):
            with self.assertRaises(CollectorSecurityError):
                update_artifact_index(suite, [])
        self.assertTrue(swapped)
        self.assertEqual(list((outside / "suite").iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "ancestor symlink race requires POSIX")
    def test_source_parent_replaced_after_path_check_cannot_copy_external_bytes(self):
        bridge = self.root / "source-bridge"
        source_root = bridge / "runtime"
        source = source_root / "evidence" / "abi.json"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"expected evidence")
        outside = self.root / "outside-source"
        outside_source = outside / "runtime" / "evidence" / "abi.json"
        outside_source.parent.mkdir(parents=True)
        outside_source.write_bytes(b"private content")
        destination_root = self.root / "destination"
        destination_root.mkdir()
        destination = destination_root / "abi.json"
        real_guard = is_path_safe
        swapped = False

        def swap_parent_after_check(base_dir, target_path):
            nonlocal swapped
            safe = real_guard(base_dir, target_path)
            if target_path == source and safe and not swapped:
                bridge.rename(self.root / "original-source-bridge")
                bridge.symlink_to(outside, target_is_directory=True)
                swapped = True
            return safe

        with patch("ops.a8.collector.is_path_safe", side_effect=swap_parent_after_check):
            with self.assertRaises(CollectorSecurityError):
                copy_checked_runtime_file(source_root, source, destination_root, destination)
        self.assertTrue(swapped)
        self.assertFalse(destination.exists())
        self.assertEqual(outside_source.read_bytes(), b"private content")

    @unittest.skipUnless(os.name == "posix", "descriptor-relative index publication requires POSIX")
    def test_suite_root_replaced_during_index_write_preserves_outside_and_removes_temporary_file(self):
        outside = self.root / "outside"
        outside.mkdir()
        moved = self.root / "moved-suite"
        real_fsync = os.fsync

        def swap_after_flush(fd):
            real_fsync(fd)
            self.suite_dir.rename(moved)
            self.suite_dir.symlink_to(outside, target_is_directory=True)

        with patch("ops.a8.collector.os.fsync", side_effect=swap_after_flush):
            with self.assertRaises(CollectorSecurityError):
                update_artifact_index(self.suite_dir, [])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "descriptor-relative index publication requires POSIX")
    def test_substituted_index_temporary_file_cannot_be_published(self):
        real_fsync = os.fsync
        substituted = None

        def replace_temp_after_flush(fd):
            nonlocal substituted
            real_fsync(fd)
            candidates = list(self.suite_dir.glob(".artifact-index-*"))
            self.assertEqual(len(candidates), 1)
            substituted = candidates[0]
            substituted.rename(self.root / "original-index-temp")
            substituted.write_bytes(b"synthetic attacker index")

        with patch("ops.a8.collector.os.fsync", side_effect=replace_temp_after_flush):
            with self.assertRaises(CollectorSecurityError):
                update_artifact_index(self.suite_dir, [])
        self.assertIsNotNone(substituted)
        self.assertFalse((self.suite_dir / "artifact-index.json").exists())
        self.assertEqual(substituted.read_bytes(), b"synthetic attacker index")


if __name__ == "__main__":
    unittest.main()
