"""Unit tests for offline suite runtime evidence reconciliation without Docker daemon.

Tests cover bounded run IDs, artifact conflicts, symlink rejection, and
reconstruction of a partially written artifact index without recopying evidence.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.a8.models import (
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
    make_task_run_id,
)
from ops.a8.orchestrator import reconcile_suite_runtime_evidence
from ops.a8 import collector
from ops.a8.collector import copy_runtime_artifact, is_path_safe
from ops.a8.runtime import sha256_file
from ops.a8.tests.real_fixtures import (
    GONKA_SOURCE_SHA,
    GONKA_TREE_SHA,
    LOCK_EXACT_E_CHECKPOINTS,
    MARKETPLACE_SHA,
    MARKETPLACE_TREE_SHA,
    real_lock_exact_e_context_for_run,
    runtime_identity_document,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-recov-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.workspace = self.root / "workspace"

        self.workspace.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_synthetic_staged_suite(
        self,
        suite_id: str,
        create_completed_collection: bool = True,
    ) -> Path:
        staged_dir = self.workspace / "suites" / suite_id
        staged_dir.mkdir(parents=True, exist_ok=True)

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
        )
        plan = SuitePlan("1.0.0", suite_id, "2026-09-11T12:00:00Z", "smoke", None, source_id, [task])
        (staged_dir / "suite-plan.json").write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")

        # Synthetic E2E context using recorded business phases.
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        run_dir = staged_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        ctx_file = run_dir / "evidence" / run_id / "live-context.json"
        ctx_file.parent.mkdir(parents=True)
        ctx_file.write_text(json.dumps(real_lock_exact_e_context_for_run(run_id)), encoding="utf-8")
        ident_file = run_dir / "identity.json"
        ident_file.write_text(json.dumps(runtime_identity_document(run_id)), encoding="utf-8")

        if create_completed_collection:
            task_res = TaskResult(
                task_id="lock-exact-e",
                ordinal=1,
                run_id=run_id,
                proof_level=ProofLevel.NATIVE,
                execution_status=ExecutionStatus.PASSED,
                evidence_status=EvidenceStatus.COMPLETE,
                cleanup_status=CleanupStatus.CLEANED,
                acceptance_status=AcceptanceStatus.NOT_REVIEWED,
                start_time_utc="2026-09-11T12:00:00Z",
                end_time_utc="2026-09-11T12:05:00Z",
                duration_seconds=300.0,
                phase="COMPLETED",
                exit_code=0,
                primary_failure=None,
                secondary_errors=[],
                expected_cases=list(LOCK_EXACT_E_CHECKPOINTS),
                observed_passed_cases=list(LOCK_EXACT_E_CHECKPOINTS),
                missing_cases=[],
                missing_artifacts=[],
                raw_evidence_dir=str(run_dir),
                exported_evidence_dir=str(run_dir),
            )
            suite_res = SuiteResult(
                schema_version="1.0.0",
                suite_id=suite_id,
                created_at_utc="2026-09-11T12:00:00Z",
                completed_at_utc="2026-09-11T12:05:00Z",
                source_identity=source_id,
                overall_status=ExecutionStatus.PASSED,
                tasks=[task_res],
                summary_message=f"Suite {suite_id} finished",
            )
            (staged_dir / "suite-result.json").write_text(json.dumps(suite_res.to_dict(), indent=2), encoding="utf-8")
            (run_dir / "result.json").write_text(json.dumps(task_res.to_dict(), indent=2), encoding="utf-8")

            index_doc = {
                "schema_version": "1.0.0",
                "total_artifacts": 2,
                "artifacts": [
                    {
                        "relative_path": f"runs/{run_id}/identity.json",
                        "size_bytes": ident_file.stat().st_size,
                        "sha256": sha256_file(ident_file),
                        "run_id": run_id,
                        "task_id": "lock-exact-e",
                        "artifact_kind": "METADATA_JSON",
                    },
                    {
                        "relative_path": f"runs/{run_id}/evidence/{run_id}/live-context.json",
                        "size_bytes": ctx_file.stat().st_size,
                        "sha256": sha256_file(ctx_file),
                        "run_id": run_id,
                        "task_id": "lock-exact-e",
                        "artifact_kind": "METADATA_JSON",
                    }
                ],
            }
            (staged_dir / "artifact-index.json").write_text(json.dumps(index_doc, indent=2), encoding="utf-8")

        return staged_dir

    def test_recovery_preserves_hyphenated_task_identity_when_the_suite_id_is_truncated(self):
        suite_id = "review-round-three-with-a-suite-id-longer-than-thirty-characters"
        staged_dir = self._create_synthetic_staged_suite(suite_id, create_completed_collection=False)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        self.assertGreater(len(suite_id), 30)
        self.assertEqual(run_id, f"{suite_id[:30]}-01-lock-exact-e")
        # The identity exists only in runtime, so recovery must actually copy it.
        (staged_dir / "runs" / run_id / "identity.json").unlink()

        # Un-staged runtime directory from interrupted step
        runtime_root = self.workspace / "runtime"
        r_dir = runtime_root / run_id
        r_dir.mkdir(parents=True)

        ident_data = runtime_identity_document(run_id)
        (r_dir / "identity.json").write_text(json.dumps(ident_data), encoding="utf-8")
        (r_dir / "launcher.log").write_text("launcher log content", encoding="utf-8")

        # Also create a neighbor directory belonging to another suite (should NOT be captured)
        neighbor_dir = runtime_root / "other-suite-01-task"
        neighbor_dir.mkdir(parents=True)
        (neighbor_dir / "identity.json").write_text(json.dumps(runtime_identity_document("other-suite-01-task")), encoding="utf-8")

        ok, errs = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertTrue(ok, errs)

        # Verify artifact-index.json correctly classifies task_id as 'lock-exact-e', NOT 'e'
        index_file = staged_dir / "artifact-index.json"
        self.assertTrue(index_file.is_file())
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
        entries = index_data.get("artifacts", [])
        self.assertCountEqual(
            [entry["relative_path"] for entry in entries],
            [f"runs/{run_id}/identity.json", f"runs/{run_id}/launcher.log"],
        )
        for name in ("identity.json", "launcher.log"):
            self.assertEqual((staged_dir / "runs" / run_id / name).read_bytes(), (r_dir / name).read_bytes())
        for entry in entries:
            self.assertEqual(entry["task_id"], "lock-exact-e")
            self.assertEqual(entry["run_id"], run_id)

        # Verify neighbor directory was NOT reconciled
        self.assertFalse((staged_dir / "runs" / "other-suite-01-task").exists())

    def test_reconcile_suite_runtime_evidence_refuses_to_overwrite_existing_artifact_when_runtime_diverges(self):
        suite_id = "suite-recov-conflict-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        collected_identity = staged_dir / "runs" / run_id / "identity.json"
        original_bytes = collected_identity.read_bytes()

        runtime_root = self.workspace / "runtime"
        r_dir = runtime_root / "runs" / run_id
        r_dir.mkdir(parents=True, exist_ok=True)
        mutated = json.loads(original_bytes)
        mutated["gonka_commit_sha"] = "0" * 40
        (r_dir / "identity.json").write_text(json.dumps(mutated), encoding="utf-8")

        ok, errs = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertFalse(ok)
        self.assertTrue(any("conflicts with already-collected suite artifact" in e for e in errs), errs)
        self.assertEqual(collected_identity.read_bytes(), original_bytes)

    def test_reconcile_rejects_runtime_file_replaced_by_private_symlink_after_preflight(self):
        suite_id = "suite-recov-source-race-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id, create_completed_collection=False)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        runtime_root = self.workspace / "runtime"
        run_dir = runtime_root / "runs" / run_id
        run_dir.mkdir(parents=True)
        source = run_dir / "launcher.log"
        source.write_bytes(b"public runtime log")
        private = self.root / "private.log"
        private.write_bytes(b"synthetic private material")
        destination = staged_dir / "runs" / run_id / "launcher.log"
        real_guard = is_path_safe
        substituted = False

        def swap_after_check(base_dir, target_path):
            nonlocal substituted
            safe = real_guard(base_dir, target_path)
            if target_path == source and safe and not substituted:
                substituted = True
                source.rename(run_dir / "original-launcher.log")
                source.symlink_to(private)
            return safe

        with patch("ops.a8.orchestrator.is_path_safe", side_effect=swap_after_check):
            ok, errors = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertTrue(substituted)
        self.assertFalse(ok)
        self.assertTrue(errors)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.is_symlink())
        self.assertEqual(private.read_bytes(), b"synthetic private material")

    def test_reconcile_rejects_index_path_traversal_before_reading_outside_suite(self):
        suite_id = "suite-recov-index-traversal-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id)
        private = self.workspace / "private.json"
        private.write_bytes(b"synthetic private material")
        index = staged_dir / "artifact-index.json"
        document = json.loads(index.read_text(encoding="utf-8"))
        malicious = dict(document["artifacts"][0])
        malicious["relative_path"] = "../../private.json"
        malicious["sha256"] = sha256_file(private)
        document["artifacts"].append(malicious)
        document["total_artifacts"] = len(document["artifacts"])
        index.write_text(json.dumps(document), encoding="utf-8")

        with patch("ops.a8.collector._open_regular_source", wraps=collector._open_regular_source) as open_source:
            ok, errors = reconcile_suite_runtime_evidence(
                staged_dir, self.workspace / "runtime", suite_id,
            )
        self.assertFalse(ok)
        self.assertTrue(any("artifact-index" in error for error in errors), errors)
        self.assertFalse(any(Path(call.args[1]).resolve() == private for call in open_source.call_args_list))

    def test_reconcile_suite_runtime_evidence_rejects_destination_symlink_before_any_write(self):
        suite_id = "suite-recov-symlink-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id, create_completed_collection=False)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        outside_file = self.root / "outside_target.json"
        outside_file.write_text("SECRET_CONTENT", encoding="utf-8")

        dest_identity = staged_dir / "runs" / run_id / "identity.json"
        dest_identity.unlink()
        dest_identity.symlink_to(outside_file)

        runtime_root = self.workspace / "runtime"
        r_dir = runtime_root / "runs" / run_id
        r_dir.mkdir(parents=True, exist_ok=True)
        (r_dir / "identity.json").write_text(json.dumps(runtime_identity_document(run_id)), encoding="utf-8")

        # This candidate sorts before the symlink-bearing run and is eligible
        # for copying. A late per-file rejection must not leave it staged.
        earlier_run = runtime_root / "runs" / suite_id
        earlier_run.mkdir()
        (earlier_run / "identity.json").write_text(json.dumps(runtime_identity_document(suite_id)), encoding="utf-8")
        (earlier_run / "launcher.log").write_text("must not be copied", encoding="utf-8")

        def staged_tree_snapshot():
            snapshot = {}
            for path in [staged_dir, *staged_dir.rglob("*")]:
                if path.is_symlink():
                    content = str(path.readlink())
                elif path.is_file():
                    content = path.read_bytes()
                else:
                    content = None
                metadata = path.lstat()
                snapshot[path.relative_to(staged_dir).as_posix()] = (
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    content,
                )
            return snapshot

        original_tree = staged_tree_snapshot()

        with patch("ops.a8.orchestrator.copy_runtime_artifact", wraps=copy_runtime_artifact) as copy_file:
            ok, errs = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertFalse(ok)
        self.assertTrue(any("symlink" in e.lower() for e in errs), errs)
        copy_file.assert_not_called()
        self.assertEqual(staged_tree_snapshot(), original_tree)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "SECRET_CONTENT")

    def test_reconcile_suite_runtime_evidence_indexes_copied_file_when_interrupted_before_index_update(self):
        suite_id = "suite-recov-partial-index-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        (staged_dir / "runs" / run_id / "result.json").unlink()
        (staged_dir / "suite-result.json").unlink()
        ident_rel = f"runs/{run_id}/identity.json"
        dest_identity = staged_dir / ident_rel
        original_bytes = dest_identity.read_bytes()

        # Remove identity.json from artifact-index.json while keeping it on disk in staged_dir and runtime_root
        index_file = staged_dir / "artifact-index.json"
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
        original_entries = index_data["artifacts"]
        kept = [a for a in original_entries if a["relative_path"] != ident_rel]
        index_data["artifacts"] = kept
        index_data["total_artifacts"] = len(kept)
        index_file.write_text(json.dumps(index_data, indent=2) + "\n", encoding="utf-8")

        runtime_root = self.workspace / "runtime"
        r_dir = runtime_root / "runs" / run_id
        r_dir.mkdir(parents=True, exist_ok=True)
        (r_dir / "identity.json").write_bytes(original_bytes)

        # Identical bytes alone cannot detect copy2 overwriting the existing file.
        with patch("ops.a8.orchestrator.copy_runtime_artifact", wraps=copy_runtime_artifact) as copy_file:
            ok, errs = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertTrue(ok, errs)
        self.assertEqual(errs, [])
        copy_file.assert_not_called()
        self.assertEqual(dest_identity.read_bytes(), original_bytes)

        updated_index = json.loads(index_file.read_text(encoding="utf-8"))
        # Compare complete entries, including hashes, sizes, ownership and kind;
        # the surviving entry must also remain intact, with no duplicates.
        self.assertCountEqual(updated_index["artifacts"], original_entries)
        self.assertEqual(updated_index["total_artifacts"], len(original_entries))

    def test_reconcile_suite_runtime_evidence_rejects_identical_mutation_of_suite_and_runtime_against_pinned_index(self):
        suite_id = "suite-recov-pinned-index-01"
        staged_dir = self._create_synthetic_staged_suite(suite_id)
        run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
        dest_identity = staged_dir / "runs" / run_id / "identity.json"

        runtime_root = self.workspace / "runtime"
        r_dir = runtime_root / "runs" / run_id
        r_dir.mkdir(parents=True, exist_ok=True)
        runtime_identity = r_dir / "identity.json"

        # Mutate both suite and runtime identity.json to the same bytes while preserving artifact-index.json
        mutated = json.loads(dest_identity.read_text(encoding="utf-8"))
        mutated["gonka_commit_sha"] = "0" * 40
        mutated_bytes = (json.dumps(mutated, indent=2) + "\n").encode("utf-8")
        dest_identity.write_bytes(mutated_bytes)
        runtime_identity.write_bytes(mutated_bytes)

        ok, errs = reconcile_suite_runtime_evidence(staged_dir, runtime_root, suite_id)
        self.assertFalse(ok)
        self.assertTrue(any("sha256 mismatch" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
