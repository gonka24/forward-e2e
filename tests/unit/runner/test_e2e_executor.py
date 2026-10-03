"""Unit tests for the E2E executor: replay identity, portable source packages,
offline provenance re-verification and fail-closed build manifests.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import signal
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from typing import Callable, Sequence, Union
from unittest.mock import MagicMock, patch

from forward_e2e.execution import context as context_module
from forward_e2e.execution import outcome as outcome_module
from forward_e2e.execution.compat import marketplace_contracts_adapter
from forward_e2e.execution.context import E2ERunContext
from forward_e2e.execution.errors import (
    BuildProvenanceError,
    SuiteExecutionError,
    IntegrityError,
    ExecutionCancelled,
    UnsupportedCompatibilityError,
    LockIntegrityError,
    ObservedVersionError,
    RunnerImageError,
    PathSafetyError,
    SourceNotPristineError,
    SourceAcquisitionError,
    SourceSnapshotMutatedError,
)
from forward_e2e.execution.evidence import (
    EVIDENCE_MISSING,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_SATISFIED,
    LIVE_CONTEXT_FILENAME,
    requirements_from_documents,
)
from forward_e2e.execution.executor import (
    ExecutionRequest,
    assert_runner_matches_lock,
    rebuild_adapter,
    grade_run_package,
    _verify_post_run_provenance,
    execute_plan,
    restore_source,
)
from forward_e2e.execution.gitio import GitClient
from forward_e2e.execution.outcome import RUN_RESULT_FILENAME, RunStatus
from forward_e2e.execution.cancel import Cancellation
from forward_e2e.execution.planner import _selection_section, RunnerLayout, HARNESS_FILES, VERIFIER_FILES, hash_runner_files
from forward_e2e.suite.catalog import compute_catalog_hash, resolve_e2e_selection
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    BuildManifest,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    copy_lock_into_run,
    content_sha256,
    load_run_lock,
    write_run_lock,
)
from forward_e2e.execution.runner_image import RunnerImageIdentity
from forward_e2e.execution.sources import bundle_ref
from forward_e2e.suite.evidence_model import EVIDENCE_MODEL_IMMUTABLE
from forward_e2e.suite.models import ExecutionStatus
from tests.unit.runner.support.fakes import (
    GONKA_TREE_SHA,
    IMMUTABLE_EVIDENCE_FILES,
    MARKETPLACE_TREE_SHA,
    FakeAcquirer,
    RecordingGitRunner,
    StubLayout,
    fake_process_runner,
    live_context_payload,
    make_test_adapter,
    ownership_payload,
    snapshot_fingerprint,
    source_immutability_bytes,
    write_suite_output,
)
from tests.unit.runner.support.packages import make_baseline_lock

GONKA_SHA = "1" * 40
CONTRACTS_SHA = "2" * 40
OTHER_SHA = "3" * 40
RUNNER_IMAGE_ID = "sha256:" + "a" * 64
CHAIN_IMAGE_ID = "sha256:" + "b" * 64
FOREIGN_IMAGE_ID = "sha256:" + "c" * 64

#: The single native scenario these fixtures select.
NATIVE_TASK_ID = "lock-exact-e"
#: A boundary scenario, used to prove that a probe which never starts a chain
#: owes no live context (review finding F1).
BOUNDARY_TASK_ID = "wasm-abi-boundary"

#: Where ``forward_e2e/execution/evidence.py`` looks for one task's live context, relative
#: to that task's own run directory inside the suite. The evidence policy
#: recognises all candidate layouts (the nested producer layout, the direct
#: producer layout, and the legacy flat layout).
POLICY_LIVE_CONTEXT_RELPATH = "live-context.json"
#: The direct producer layout where ``forward_e2e/suite/runtime.py`` and ``collector.py``
#: export evidence under ``evidence/live-context.json``.
PRODUCER_LIVE_CONTEXT_RELPATH = "evidence/live-context.json"



# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------
def make_lock(
    *,
    gonka_bundle_sha256: str,
    contracts_bundle_sha256: str,
    scenarios=(NATIVE_TASK_ID,),
) -> RunLock:
    """A minimal but complete lock, built via the canonical baseline builder."""
    return make_baseline_lock(
        scenarios=scenarios,
        plan_id="plan-0001",
        gonka_sha=GONKA_SHA,
        contracts_sha=CONTRACTS_SHA,
        gonka_bundle_sha256=gonka_bundle_sha256,
        contracts_bundle_sha256=contracts_bundle_sha256,
        gonka_bundle_ref=bundle_ref("gonka"),
        contracts_bundle_ref=bundle_ref("contracts"),
        harness_hash="f" * 64,
        verifier_hash="e" * 64,
        catalog_hash="d" * 64,
        adapter_content_hashes=("9" * 64, "8" * 64),
        limits={},
    )


make_adapter = make_test_adapter


class E2EExecutorTests(unittest.TestCase):
    def setUp(self):
        # execute_plan restores semantic variables in the real process environment.
        # unittest cleanups restore it even if setup or a test raises.
        self.enterContext(patch.dict(os.environ))
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-exec-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.package_dir = self.root / "package"
        self.output_dir = self.root / "out"
        self.workspace_dir = self.root / "work"
        self.runner_root = self.root / "runner"
        (self.package_dir / "bundles").mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        self.workspace_dir.mkdir(parents=True)
        (self.runner_root / "scripts").mkdir(parents=True)

        self.gonka_bundle_bytes = b"GIT-BUNDLE-SYNTHETIC-GONKA"
        self.contracts_bundle_bytes = b"GIT-BUNDLE-SYNTHETIC-CONTRACTS"
        (self.package_dir / "bundles" / "gonka.bundle").write_bytes(self.gonka_bundle_bytes)
        (self.package_dir / "bundles" / "contracts.bundle").write_bytes(self.contracts_bundle_bytes)

        self.lock = make_lock(
            gonka_bundle_sha256=hashlib.sha256(self.gonka_bundle_bytes).hexdigest(),
            contracts_bundle_sha256=hashlib.sha256(self.contracts_bundle_bytes).hexdigest(),
        )
        self.lock_path = write_run_lock(self.lock, self.package_dir / RUN_LOCK_FILENAME)
        FakeAcquirer.instances = []

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def observed_image(self, image_id: str = RUNNER_IMAGE_ID) -> RunnerImageIdentity:
        return RunnerImageIdentity(
            locator="a8-runner:local",
            image_id=image_id,
            repo_digest=None,
            locator_is_immutable=False,
            portability="local-image-only",
            resolved_by="launcher-injected",
        )

    def make_request(self, **overrides) -> ExecutionRequest:
        params = dict(
            lock=self.lock,
            lock_path=self.lock_path,
            package_dir=self.package_dir,
            output_dir=self.output_dir,
            workspace_dir=self.workspace_dir,
        )
        params.update(overrides)
        return ExecutionRequest(**params)

    def run_executor(self, request, *, suite_exit: int = 0, prepared_error=None,
                     image_id: str = RUNNER_IMAGE_ID, acquirer=FakeAcquirer,
                     live_context_relpath: Union[str, Callable[[str], str]] = POLICY_LIVE_CONTEXT_RELPATH,
                     write_live_context: bool = True,
                     live_context_payload_for=None,
                     extra_live_contexts: Sequence[str] = (),
                     during_suite=None,
                     after_suite=None,
                     snapshot_capture=None):
        """Execute a plan with every expensive collaborator replaced.

        The suite runner is a fake, but what it leaves on disk is not: it writes
        the suite plan, the suite result and the per-task evidence directories
        that ``execute_plan`` now reads back and grades. Without them the run is
        graded INCOMPLETE, which is the correct verdict for an empty directory
        and would say nothing about replay, locks or source packages.
        """
        self.suite_calls: list = []

        def suite_runner(**kwargs):
            self.suite_calls.append(kwargs)
            if during_suite is not None:
                during_suite(kwargs)
            suite_dir = write_suite_output(
                output_dir=kwargs["output_dir"],
                suite_id=kwargs["suite_id"],
                profile=kwargs["profile"],
                scenarios=kwargs["scenarios"],
                tasks=kwargs["tasks"],
                e2e_context=kwargs["e2e_context"],
                live_context_relpath=live_context_relpath,
                write_live_context=write_live_context,
                ownership_image=RUNNER_IMAGE_ID,
                live_context_payload_for=live_context_payload_for,
            )
            for extra_relpath in extra_live_contexts:
                extra_file = suite_dir / extra_relpath
                extra_file.parent.mkdir(parents=True, exist_ok=True)
                extra_file.write_text(
                    json.dumps(
                        live_context_payload(
                            run_id="extra",
                            gonka_sha=GONKA_SHA,
                            runtime_sha=GONKA_SHA,
                        ),
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if after_suite is not None:
                after_suite(suite_dir)
            return suite_exit

        def default_capture(worktree: Path) -> dict:
            if prepared_error is not None:
                raise prepared_error
            if Path(worktree).name == "gonka":
                return snapshot_fingerprint(GONKA_SHA, GONKA_TREE_SHA)
            return snapshot_fingerprint(CONTRACTS_SHA, MARKETPLACE_TREE_SHA)

        git_mock = MagicMock(name="git-client")
        git_mock.stdout.return_value = f"{GONKA_SHA[:7]}\n"
        with patch("forward_e2e.execution.executor.RunnerLayout", StubLayout), \
                patch("forward_e2e.execution.executor.resolve_runner_image",
                      return_value=self.observed_image(image_id)), \
                patch("forward_e2e.execution.executor.assert_runner_matches_lock", return_value=None), \
                patch("forward_e2e.execution.executor.rebuild_adapter",
                      side_effect=lambda role, lock, layout: make_adapter(role, with_build=True)), \
                patch("forward_e2e.execution.executor.SourceAcquirer", acquirer) as acquirer_mock:
            self.acquirer_mock = acquirer_mock
            return execute_plan(
                request,
                git=git_mock,
                build_runner=fake_process_runner,
                docker_runner=fake_process_runner,
                runner_root=self.runner_root,
                env={},
                emit=lambda message: None,
                suite_runner=suite_runner,
                snapshot_capture=snapshot_capture or default_capture,
            )

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_the_executor_persists_the_environment_record_even_when_runtime_preparation_fails(self):
        payload = self.lock.to_dict()
        payload["semantic_inputs"] = {
            "semantic_environment": {"A8_PROOF_LEVEL": "native"},
            "operational_environment_names": [],
        }
        self.lock = RunLock.from_dict(payload)
        # Create a new lock instead of rewriting the immutable setup fixture.
        self.lock_path = write_run_lock(self.lock, self.package_dir / "environment.lock.json")
        run_id = "environment-record-before-failure"
        primary = BuildProvenanceError("Synthetic preparation failure")
        with patch.dict(
            os.environ,
            {"A8_PROOF_LEVEL": "smoke", "A8_EXPECTED_PROTO_SHA": "0" * 64},
            clear=True,
        ):
            with self.assertRaises(BuildProvenanceError) as caught:
                self.run_executor(self.make_request(run_id=run_id), prepared_error=primary)
        self.assertIs(caught.exception, primary)
        for root in (self.workspace_dir / run_id / "run", self.output_dir / run_id):
            with self.subTest(root=root):
                record = self.read_json(root / BUILD_MANIFEST_FILENAME)["execution_environment"]
                self.assertEqual(record["restored"], ["A8_PROOF_LEVEL"])
                self.assertEqual(record["overridden_from_shell"], ["A8_PROOF_LEVEL"])
                self.assertEqual(record["cleared_ambient"], ["A8_EXPECTED_PROTO_SHA"])

    def test_a_nonzero_suite_exit_remains_a_failure_after_report_and_recovery_even_with_green_evidence(self):
        from forward_e2e.execution.cli import cmd_recover, cmd_report, parse_e2e_args

        for code in (1, 2):
            with self.subTest(code=code):
                run_id = f"suite-error-{code}"
                with self.assertRaises(SuiteExecutionError) as caught:
                    self.run_executor(self.make_request(run_id=run_id), suite_exit=code)
                self.assertEqual(caught.exception.details["suite_exit_code"], code)
                root = self.output_dir / run_id
                build = self.read_json(root / BUILD_MANIFEST_FILENAME)
                self.assertEqual(build["failures"][0]["code"], "SUITE_EXECUTION_FAILED")
                self.assertEqual(build["failures"][0]["details"]["suite_exit_code"], code)
                self.assertEqual(self.read_json(root / RUN_RESULT_FILENAME)["status"], "FAILED")
                _, args = parse_e2e_args(["report", "--run", str(root)])
                self.assertEqual(cmd_report(args, emit=lambda message: None), 1)
                recovered = self.root / f"recovered-{code}"
                _, args = parse_e2e_args([
                    "recover", "--run", run_id, "--workspace", str(self.workspace_dir),
                    "--output", str(recovered),
                ])
                self.assertEqual(cmd_recover(args, emit=lambda message: None), 1)
                self.assertEqual(self.read_json(recovered / run_id / RUN_RESULT_FILENAME)["status"], "FAILED")

    @contextmanager
    def fail_initial_delivery_write(self, run_id):
        """Fail the staging marker before any destination export can begin."""
        from forward_e2e.execution import runlock

        expected_path = self.workspace_dir / run_id / "run" / DELIVERY_MANIFEST_FILENAME
        real_write = runlock.atomic_write_bytes
        failed_paths = []

        def fail_write(target, *args, **kwargs):
            if Path(target) == expected_path:
                failed_paths.append(Path(target))
                raise OSError("Synthetic delivery storage failure")
            return real_write(target, *args, **kwargs)

        with patch.object(runlock, "atomic_write_bytes", side_effect=fail_write):
            yield
        self.assertEqual(failed_paths, [expected_path])
        # Live operational status is published before the evidence export.
        destination = self.output_dir / run_id
        self.assertEqual(self.read_json(destination / "status.json")["export_status"], "FAILED")
        self.assertFalse((destination / DELIVERY_MANIFEST_FILENAME).exists())

    def test_an_initial_delivery_write_error_preserves_the_primary_failure_and_grades_staging_with_both_errors(self):
        run_id = "delivery-initial-write-error"
        primary = BuildProvenanceError("Synthetic build boundary failure")

        with self.fail_initial_delivery_write(run_id):
            with self.assertRaises(BuildProvenanceError) as caught:
                self.run_executor(self.make_request(run_id=run_id), prepared_error=primary)
        self.assertIs(caught.exception, primary)
        verdict = self.read_json(self.workspace_dir / run_id / "run" / RUN_RESULT_FILENAME)
        self.assertEqual(verdict["status"], "FAILED")
        self.assertIn("BUILD_PROVENANCE_FAILED", [f["code"] for f in verdict["findings"]])
        self.assertTrue(any("Synthetic delivery storage failure" in f["message"] for f in verdict["findings"]))

    def test_a_delivery_initialization_error_on_a_successful_build_still_writes_a_failed_staging_verdict(self):
        run_id = "delivery-only-error"

        with self.fail_initial_delivery_write(run_id):
            self.assertEqual(self.run_executor(self.make_request(run_id=run_id)), 1)
        verdict = self.read_json(self.workspace_dir / run_id / "run" / RUN_RESULT_FILENAME)
        self.assertNotEqual(verdict["status"], "PASSED")
        self.assertTrue(any("Synthetic delivery storage failure" in f["message"] for f in verdict["findings"]))

    def test_failure_to_write_the_fallback_verdict_does_not_replace_the_primary_execution_error(self):
        run_id = "verdict-write-error"
        primary = BuildProvenanceError("Synthetic primary failure")
        verdict_path = self.workspace_dir / run_id / "run" / RUN_RESULT_FILENAME
        real_write = outcome_module.atomic_write_bytes

        def fail_verdict_write(path, *args, **kwargs):
            if Path(path) == verdict_path:
                raise OSError("Synthetic verdict storage failure")
            return real_write(path, *args, **kwargs)

        with self.fail_initial_delivery_write(run_id), \
                patch.object(outcome_module, "atomic_write_bytes", fail_verdict_write):
            with self.assertRaises(BuildProvenanceError) as caught:
                self.run_executor(self.make_request(run_id=run_id), prepared_error=primary)
        self.assertIs(caught.exception, primary)
        self.assertIn("Synthetic verdict storage failure", str(caught.exception.__cause__))

    def test_suite_signal_exit_codes_are_preserved_as_cancellation_in_run_and_report(self):
        for code in (130, 143):
            with self.subTest(code=code):
                run_id = f"cancel-suite-{code}"
                with self.assertRaises(ExecutionCancelled) as cm:
                    self.run_executor(self.make_request(run_id=run_id), suite_exit=code)
                self.assertEqual(cm.exception.exit_code, code)
                root = self.output_dir / run_id
                self.assertEqual(self.read_json(root / RUN_RESULT_FILENAME)["status"], "CANCELLED")
                outcome = grade_run_package(root)
                self.assertEqual(outcome.status, RunStatus.CANCELLED)
                self.assertEqual(outcome.exit_code, code)

    def test_a_cancellation_requested_at_suite_completion_cannot_be_graded_as_passed(self):
        token = Cancellation()
        with self.assertRaises(ExecutionCancelled):
            self.run_executor(
                self.make_request(run_id="cancel-after-suite", cancellation=token),
                after_suite=lambda root: token.request(signal.SIGTERM),
            )
        root = self.output_dir / "cancel-after-suite"
        self.assertEqual(grade_run_package(root).status, RunStatus.CANCELLED)
        self.assertTrue(self.read_json(root / BUILD_MANIFEST_FILENAME)["cancellation"]["requested"])

    def test_an_absent_artifact_index_is_not_advertised_by_the_execution_manifest(self):
        self.run_executor(
            self.make_request(run_id="no-index"),
            after_suite=lambda root: (root / "artifact-index.json").unlink(),
        )
        execution = self.read_json(self.output_dir / "no-index" / EXECUTION_MANIFEST_FILENAME)
        self.assertIsNone(execution["artifact_index_relpath"])

    def test_an_unsafe_artifact_index_still_seals_the_run_and_records_delivery_and_verdict(self):
        external = self.root / "external-index.json"

        def replace_index(root):
            index = root / "artifact-index.json"
            index.rename(external)
            index.symlink_to(external)

        run_id = "unsafe-index-finalization"
        with self.assertRaises(PathSafetyError):
            self.run_executor(self.make_request(run_id=run_id), after_suite=replace_index)

        stage = self.workspace_dir / run_id / "run"
        build = self.read_json(stage / BUILD_MANIFEST_FILENAME)
        execution = self.read_json(stage / EXECUTION_MANIFEST_FILENAME)
        self.assertEqual(execution["build_manifest_sha256"], content_sha256(build))
        self.assertIsNone(execution["artifact_index_relpath"])
        self.assertIn("PATH_SAFETY_VIOLATION", [f["code"] for f in build["failures"]])
        delivery = self.read_json(stage / "delivery.json")
        self.assertEqual(delivery["status"], "FAILED")
        self.assertTrue(delivery["attempts"][0]["completed_at_utc"])
        verdict = self.read_json(stage / RUN_RESULT_FILENAME)
        self.assertNotEqual(verdict["status"], "PASSED")
        self.assertIn("PATH_SAFETY_VIOLATION", [f["code"] for f in verdict["findings"]])
        self.assertTrue(external.is_file())

    def test_missing_runner_hashes_are_rejected_even_in_a_valid_lock_envelope(self):
        from forward_e2e.execution.planner import EXTERNAL_TEST_DIRS, NETWORK_FILES, _external_tests_section, _network_section
        layout = RunnerLayout(self.root / "hash-runner")
        for relative in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES:
            target = layout.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic runner file\n")
        for _, rel in EXTERNAL_TEST_DIRS:
            ext_dir = layout.root / rel
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "placeholder.txt").write_text("ok\n", encoding="utf-8")
        layout.runner_version_file.parent.mkdir(parents=True, exist_ok=True)
        layout.runner_version_file.write_text("a8-runner/2.0.0\n", encoding="utf-8")
        self.lock.external_tests = _external_tests_section(layout)
        self.lock.network = _network_section(layout)
        self.lock.runner.update(
            repo_url="https://example.org/runner.git",
            commit_sha="3" * 40,
            tree_sha="4" * 40,
            harness_hash=hash_runner_files(layout.root, HARNESS_FILES),
            verifier_hash=hash_runner_files(layout.root, VERIFIER_FILES),
            catalog_hash=compute_catalog_hash(),
        )
        (layout.root / "runner-source.json").write_text(json.dumps({
            "schema_version": 1, "repo_url": "https://example.org/runner.git",
            "commit_sha": "3" * 40, "tree_sha": "4" * 40,
        }), encoding="utf-8")
        assert_runner_matches_lock(self.lock, layout)
        for key in ("harness_hash", "verifier_hash", "catalog_hash", "commit_sha", "tree_sha", "repo_url"):
            for value in (None, "", "0" * 64):
                with self.subTest(key=key, value=value):
                    lock = RunLock.from_dict(self.lock.to_dict())
                    if value is None:
                        del lock.runner[key]
                    else:
                        lock.runner[key] = value
                    path = self.root / f"{key}-{value or 'absent'}-{value is None}.json"
                    loaded = load_run_lock(write_run_lock(lock, path))
                    with self.assertRaises(IntegrityError):
                        assert_runner_matches_lock(loaded, layout)

    def test_an_adapter_hash_cannot_be_omitted_or_empty_to_disable_drift_detection(self):
        adapter = marketplace_contracts_adapter()
        self.lock.compatibility["contracts"] = adapter.lock_record(match_mode="markers", measurements={})
        layout = RunnerLayout(self.runner_root)
        self.assertEqual(rebuild_adapter("contracts", self.lock, layout), adapter)
        for value in (None, "", "0" * 64):
            with self.subTest(value=value):
                lock = RunLock.from_dict(self.lock.to_dict())
                lock.compatibility["contracts"] = dict(self.lock.compatibility["contracts"])
                if value is None:
                    del lock.compatibility["contracts"]["adapter_content_hash"]
                else:
                    lock.compatibility["contracts"]["adapter_content_hash"] = value
                with self.assertRaises(UnsupportedCompatibilityError):
                    rebuild_adapter("contracts", lock, layout)

    # -- replay semantics ----------------------------------------------
    def test_replay_creates_a_new_run_id_and_never_touches_the_parent_evidence(self):
        parent_exit = self.run_executor(self.make_request(command="run"))
        self.assertEqual(parent_exit, 0)
        parent_dirs = list(self.output_dir.iterdir())
        self.assertEqual(len(parent_dirs), 1)
        parent_dir = parent_dirs[0]
        parent_run_id = parent_dir.name
        before = {
            path.relative_to(parent_dir).as_posix(): path.read_bytes()
            for path in sorted(parent_dir.rglob("*"))
            if path.is_file()
        }

        replay_exit = self.run_executor(
            self.make_request(command="rerun", replay=True, parent_run_id=parent_run_id)
        )
        self.assertEqual(replay_exit, 0)

        replay_dirs = [p for p in self.output_dir.iterdir() if p.name != parent_run_id]
        self.assertEqual(len(replay_dirs), 1)
        replay_dir = replay_dirs[0]
        self.assertNotEqual(replay_dir.name, parent_run_id)

        replay_manifest = self.read_json(replay_dir / EXECUTION_MANIFEST_FILENAME)
        self.assertEqual(replay_manifest["run_id"], replay_dir.name)
        self.assertTrue(replay_manifest["fresh_network_state"])
        self.assertEqual(replay_manifest["replay_of_plan"], self.lock.plan_id)
        self.assertEqual(replay_manifest["parent_run_id"], parent_run_id)
        self.assertEqual(replay_manifest["command"], "rerun")

        after = {
            path.relative_to(parent_dir).as_posix(): path.read_bytes()
            for path in sorted(parent_dir.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)

        parent_manifest = self.read_json(parent_dir / EXECUTION_MANIFEST_FILENAME)
        self.assertIsNone(parent_manifest["replay_of_plan"])
        self.assertIsNone(parent_manifest["parent_run_id"])
        self.assertTrue(parent_manifest["fresh_network_state"])

    def test_a_plain_run_records_no_replay_reference_and_declares_fresh_network_state(self):
        self.run_executor(self.make_request(command="run"))
        run_dir = next(iter(self.output_dir.iterdir()))
        self.assertEqual(
            self.suite_calls[0]["e2e_context"].gradle_user_home,
            self.workspace_dir / run_dir.name / "run" / "build" / "gradle-home",
        )
        manifest = ExecutionManifest.from_dict(
            self.read_json(run_dir / EXECUTION_MANIFEST_FILENAME)
        )
        self.assertIsNone(manifest.replay_of_plan)
        self.assertTrue(manifest.fresh_network_state)
        self.assertEqual(manifest.lock_sha256, self.lock.lock_sha256)
        self.assertEqual(manifest.runner_image_id, RUNNER_IMAGE_ID)
        # The index points at the evidence that actually exists: the
        # orchestrator appends the suite id to the export root, and the run only
        # advertises an index once that directory is really there.
        self.assertEqual(manifest.suite_export_relpath, f"suite/{run_dir.name}")
        self.assertEqual(manifest.artifact_index_relpath, f"suite/{run_dir.name}/artifact-index.json")

    # -- portable source package ---------------------------------------
    def test_a_bundle_package_moved_to_another_directory_still_restores(self):
        relocated_root = self.root / "elsewhere"
        source_package = self.root / "portable-package"
        (source_package / "bundles").mkdir(parents=True)
        payload = b"GIT-BUNDLE-SYNTHETIC-PORTABLE"
        (source_package / "bundles" / "gonka.bundle").write_bytes(payload)
        lock = make_lock(
            gonka_bundle_sha256=hashlib.sha256(payload).hexdigest(),
            contracts_bundle_sha256="0" * 64,
        )
        shutil.move(str(source_package), str(relocated_root))
        self.assertFalse(source_package.exists())

        runner = RecordingGitRunner(commit_sha=GONKA_SHA, ref=bundle_ref("gonka"))
        acquirer = _real_acquirer(
            runner, package_dir=relocated_root, scratch_dir=self.root / "scratch-relocated"
        )

        restored = restore_source(
            "gonka",
            lock,
            acquirer=acquirer,
            package_dir=relocated_root,
            worktree_root=self.root / "src-relocated",
            allow_remote_refetch=True,
            emit=lambda message: None,
        )

        self.assertEqual(restored.origin, "package")
        self.assertEqual(restored.commit_sha, GONKA_SHA)
        self.assertEqual(restored.bundle_path, relocated_root / "bundles" / "gonka.bundle")
        self.assertTrue(restored.worktree.is_dir())

    def test_an_unreachable_remote_does_not_break_replay_from_a_complete_package(self):
        runner = RecordingGitRunner(commit_sha=GONKA_SHA, ref=bundle_ref("gonka"))
        acquirer = _real_acquirer(
            runner, package_dir=self.package_dir, scratch_dir=self.root / "scratch-offline"
        )

        restored = restore_source(
            "gonka",
            self.lock,
            acquirer=acquirer,
            package_dir=self.package_dir,
            worktree_root=self.root / "src-offline",
            allow_remote_refetch=False,
            emit=lambda message: None,
        )

        self.assertEqual(restored.origin, "package")
        self.assertEqual(restored.commit_sha, GONKA_SHA)
        for argv in runner.calls:
            self.assertNotIn("fetch", argv)
            self.assertNotIn("https://github.com/example/gonka", argv)
        self.assertFalse(
            [call for call in runner.flat_calls if "https://" in call],
            msg=f"a network git command was issued: {runner.flat_calls}",
        )

    def test_replay_refuses_a_missing_or_wrong_source_bundle_ref(self):
        for ref in (None, "refs/e2e/source/contracts"):
            for direct in (False, True):
                with self.subTest(ref=ref, direct=direct):
                    lock = make_lock(
                        gonka_bundle_sha256=hashlib.sha256(self.gonka_bundle_bytes).hexdigest(),
                        contracts_bundle_sha256=hashlib.sha256(self.contracts_bundle_bytes).hexdigest(),
                    )
                    if ref is None:
                        del lock.gonka["bundle_ref"]
                    else:
                        lock.gonka["bundle_ref"] = ref
                    runner = RecordingGitRunner(commit_sha=GONKA_SHA, ref=bundle_ref("gonka"))
                    acquirer = _real_acquirer(
                        runner, package_dir=self.package_dir,
                        scratch_dir=self.root / f"scratch-ref-{ref is None}-{direct}",
                    )
                    pre_acquired = (
                        {"gonka": SimpleNamespace(worktree=self.root, commit_sha=GONKA_SHA)}
                        if direct else None
                    )
                    with self.assertRaises(IntegrityError) as cm:
                        restore_source(
                            "gonka", lock, acquirer=acquirer, package_dir=self.package_dir,
                            worktree_root=self.root / f"src-ref-{ref is None}-{direct}",
                            allow_remote_refetch=False, emit=lambda message: None,
                            pre_acquired=pre_acquired,
                        )
                    self.assertIn("bundle_ref", str(cm.exception))
                    self.assertEqual(runner.calls, [])

    def test_an_incomplete_package_without_refetch_refuses_instead_of_guessing(self):
        (self.package_dir / "bundles" / "gonka.bundle").unlink()
        runner = RecordingGitRunner(commit_sha=GONKA_SHA, ref=bundle_ref("gonka"))
        acquirer = _real_acquirer(
            runner, package_dir=self.package_dir, scratch_dir=self.root / "scratch-missing"
        )

        with self.assertRaises(IntegrityError) as cm:
            restore_source(
                "gonka",
                self.lock,
                acquirer=acquirer,
                package_dir=self.package_dir,
                worktree_root=self.root / "src-missing",
                allow_remote_refetch=False,
                emit=lambda message: None,
            )
        self.assertIn("The source package is incomplete", str(cm.exception))
        self.assertEqual(runner.calls, [])

    # -- fail closed ----------------------------------------------------
    def test_a_failure_mid_execution_never_writes_a_complete_build_manifest(self):
        request = self.make_request()
        with self.assertRaises(BuildProvenanceError) as cm:
            self.run_executor(
                request,
                prepared_error=BuildProvenanceError(
                    "The prepared runtime does not descend from the requested commit",
                    {"requested": GONKA_SHA, "prepared": OTHER_SHA},
                ),
            )
        self.assertIn("does not descend from the requested commit", str(cm.exception))
        self.assertNotEqual(getattr(cm.exception, "exit_code", 1), 0)

        run_dir = next(iter(self.output_dir.iterdir()))
        manifest = self.read_json(run_dir / BUILD_MANIFEST_FILENAME)
        self.assertEqual(manifest["status"], ManifestStatus.FAILED)
        self.assertTrue(manifest["failures"])
        self.assertEqual(manifest["failures"][0]["code"], "BUILD_PROVENANCE_FAILED")

        execution = self.read_json(run_dir / EXECUTION_MANIFEST_FILENAME)
        self.assertTrue(any("execution failed" in note for note in execution["notes"]))
        self.assertIsNone(execution["artifact_index_relpath"])

    def test_a_dirty_source_snapshot_before_build_stops_execution_with_source_not_pristine(self):
        def dirty_capture(worktree: Path) -> dict:
            if Path(worktree).name == "gonka":
                return snapshot_fingerprint(
                    GONKA_SHA, GONKA_TREE_SHA, status=["?? local-test-net/override.yml"]
                )
            return snapshot_fingerprint(CONTRACTS_SHA, MARKETPLACE_TREE_SHA)

        with self.assertRaises(SourceNotPristineError):
            self.run_executor(
                self.make_request(run_id="dirty-before-build"),
                snapshot_capture=dirty_capture,
            )
        manifest = self.read_json(self.output_dir / "dirty-before-build" / BUILD_MANIFEST_FILENAME)
        self.assertEqual(manifest["status"], ManifestStatus.FAILED)
        self.assertEqual(manifest["failures"][0]["code"], SourceNotPristineError.code)

    def test_a_source_snapshot_mutated_during_build_stops_with_source_snapshot_mutated(self):
        gonka_calls = {"count": 0}

        def mutating_capture(worktree: Path) -> dict:
            if Path(worktree).name == "gonka":
                gonka_calls["count"] += 1
                if gonka_calls["count"] >= 2:
                    return snapshot_fingerprint(
                        GONKA_SHA, GONKA_TREE_SHA, status=[" M Makefile"]
                    )
                return snapshot_fingerprint(GONKA_SHA, GONKA_TREE_SHA)
            return snapshot_fingerprint(CONTRACTS_SHA, MARKETPLACE_TREE_SHA)

        with self.assertRaises(SourceSnapshotMutatedError):
            self.run_executor(
                self.make_request(run_id="mutated-after-build"),
                snapshot_capture=mutating_capture,
            )
        manifest = self.read_json(self.output_dir / "mutated-after-build" / BUILD_MANIFEST_FILENAME)
        self.assertEqual(manifest["status"], ManifestStatus.FAILED)
        self.assertEqual(manifest["failures"][0]["code"], SourceSnapshotMutatedError.code)
        self.assertEqual(manifest["source_immutability"]["verdict"], "VIOLATED")

    def test_a_runner_image_mismatch_aborts_before_any_source_is_touched(self):
        request = self.make_request()
        with self.assertRaises(RunnerImageError) as cm:
            self.run_executor(request, image_id=FOREIGN_IMAGE_ID, acquirer=MagicMock())
        self.assertIn(
            "The available runner image is not the one recorded in the lock",
            str(cm.exception),
        )
        self.acquirer_mock.assert_not_called()
        self.assertEqual(self.suite_calls, [])
        self.assertEqual(list(self.output_dir.iterdir()), [])

    # -- preserved lock --------------------------------------------------
    def test_the_preserved_lock_is_byte_identical_to_the_original(self):
        original_bytes = self.lock_path.read_bytes()
        self.run_executor(self.make_request())
        run_dir = next(iter(self.output_dir.iterdir()))
        preserved = run_dir / RUN_LOCK_FILENAME

        self.assertEqual(preserved.read_bytes(), original_bytes)
        self.assertEqual(self.lock_path.read_bytes(), original_bytes)
        self.assertEqual(load_run_lock(preserved).lock_sha256, self.lock.lock_sha256)
        self.assertEqual(
            self.read_json(preserved)["lock_sha256"],
            self.read_json(self.lock_path)["lock_sha256"],
        )
        manifest = self.read_json(run_dir / BUILD_MANIFEST_FILENAME)
        self.assertEqual(manifest["lock_sha256"], self.lock.lock_sha256)

    def test_copy_lock_into_run_refuses_to_overwrite_a_preserved_lock(self):
        run_dir = self.root / "isolated-run"
        run_dir.mkdir()
        first = copy_lock_into_run(self.lock_path, run_dir)
        self.assertEqual(first.read_bytes(), self.lock_path.read_bytes())
        with self.assertRaises(LockIntegrityError) as cm:
            copy_lock_into_run(self.lock_path, run_dir)
        self.assertIn("Refusing to overwrite the lock", str(cm.exception))

    # -- offline provenance re-verification ------------------------------
    #: The suite id these provenance fixtures are written under. The per-task
    #: directory names are derived from it by ``models.make_task_run_id``,
    #: exactly as the orchestrator derives them.
    FIXTURE_SUITE_ID = "e2e-fixture"

    def make_manifest(self, *, image_id: str = CHAIN_IMAGE_ID) -> BuildManifest:
        manifest = BuildManifest.start(lock=self.lock, run_id=self.FIXTURE_SUITE_ID)
        manifest.images.append(
            {
                "role": "gonka/inference-chain:e2e",
                "reference": "gonka/inference-chain:e2e",
                "image_id": image_id,
                "repo_digests": [],
                "created_epoch": 1.0,
                "step_id": "build-chain",
            }
        )
        return manifest

    def evidence_requirements(self, *, lock=None, manifest=None):
        """What the selection says each task owes, via the production helper.

        Derived from the lock rather than hand-written, so a test can never
        assert a duty the planner would not have recorded.
        """
        manifest = manifest or self.make_manifest()
        requirements, disagreements = requirements_from_documents(
            lock=lock or self.lock,
            suite_id=self.FIXTURE_SUITE_ID,
            build_produced_production_wasm=bool(
                (manifest.deployment or {}).get("production_wasm")
            ),
            suite_plan=None,
        )
        self.assertEqual(disagreements, [])
        return requirements

    def write_live_context(self, suite_dir: Path, *, runtime_sha: str,
                           requirement=None) -> Path:
        """Write one task's live context where that task's evidence belongs.

        Mirror the harness's nested output preserved by the collector,
        independently of the lookup being tested.
        """
        requirement = requirement or self.evidence_requirements()[0]
        target = (
            suite_dir / "runs" / requirement.run_id
            / "evidence" / requirement.run_id / LIVE_CONTEXT_FILENAME
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        (target.parent / "source-immutability.json").write_bytes(
            source_immutability_bytes(
                gonka_sha=GONKA_SHA,
                marketplace_sha=CONTRACTS_SHA,
                gonka_tree_sha=GONKA_TREE_SHA,
                marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            )
        )
        for rel_path, content in IMMUTABLE_EVIDENCE_FILES.items():
            dest = target.parent / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        target.write_text(
            json.dumps(
                live_context_payload(
                    run_id=requirement.run_id,
                    gonka_sha=GONKA_SHA,
                    marketplace_sha=CONTRACTS_SHA,
                    runtime_sha=runtime_sha,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    def write_ownership(self, suite_dir: Path, *, image_id: str) -> Path:
        evidence_dir = suite_dir / "runs" / self.evidence_requirements()[0].run_id / "cleanup-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        target = evidence_dir / "ownership.json"
        target.write_text(
            json.dumps(
                ownership_payload(run_id=self.evidence_requirements()[0].run_id, image_id=image_id),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return target

    def verify_provenance(self, suite_dir: Path, *, lock=None, manifest=None):
        manifest = manifest or self.make_manifest()
        return _verify_post_run_provenance(
            suite_dir,
            manifest=manifest,
            adapter=make_adapter("gonka", with_runtime=True),
            sources_root=self.root / "src" / "gonka",
            selected_sha=GONKA_SHA,
            marketplace_sha=CONTRACTS_SHA,
            requirements=self.evidence_requirements(lock=lock, manifest=manifest),
            emit=lambda message: None,
        )

    def test_post_run_provenance_accepts_evidence_that_matches_the_build(self):
        suite_dir = self.root / "suite-good"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)

        findings = self.verify_provenance(suite_dir)

        self.assertEqual(len(findings["live_contexts"]), 1)
        self.assertEqual(
            findings["live_contexts"][0]["runtime"]["gonka_source_sha"], GONKA_SHA
        )
        self.assertEqual(findings["live_contexts"][0]["task_id"], NATIVE_TASK_ID)
        self.assertEqual(findings["missing_evidence"], [])
        self.assertEqual(len(findings["containers"]), 1)
        self.assertTrue(findings["containers"][0]["findings"][0]["from_this_build"])

    def test_ownership_from_another_task_cannot_replace_missing_native_ownership(self):
        suite_dir = self.root / "missing-ownership"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        ownership = self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)
        stray = suite_dir / "runs" / "another-task" / "cleanup-evidence" / "ownership.json"
        stray.parent.mkdir(parents=True)
        ownership.rename(stray)
        result = self.verify_provenance(suite_dir)
        self.assertEqual(result["containers"], [])
        self.assertEqual(len(result["missing_evidence"]), 1)
        self.assertEqual(result["missing_evidence"][0]["task_id"], NATIVE_TASK_ID)
        self.assertIn("ownership.json", result["missing_evidence"][0]["expected_path"])

    def test_unreadable_or_incomplete_ownership_cannot_silently_remove_image_checks(self):
        suite_dir = self.root / "invalid-ownership"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        ownership = self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)
        baseline = ownership.read_text()
        for change in ("malformed", "missing-image", "empty-containers", "non-object"):
            with self.subTest(change=change):
                payload = json.loads(baseline)
                if change == "missing-image":
                    del payload["owned_containers"][0]["image"]
                elif change == "empty-containers":
                    payload["owned_containers"] = []
                elif change == "non-object":
                    payload = []
                ownership.write_text("{" if change == "malformed" else json.dumps(payload))
                with self.assertRaises((IntegrityError, BuildProvenanceError)):
                    self.verify_provenance(suite_dir)

    def test_ownership_metadata_must_confirm_the_exact_task_run_with_a_boolean_true(self):
        suite_dir = self.root / "ownership-metadata"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        ownership = self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)
        baseline = ownership.read_text()
        self.assertEqual(self.verify_provenance(suite_dir)["missing_evidence"], [])
        cases = [("run_id", value) for value in (None, "other-task", "")]
        cases += [("ownership_verified", value) for value in (None, False, 1, "true")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = json.loads(baseline)
                if value is None:
                    del payload[field]
                else:
                    payload[field] = value
                ownership.write_text(json.dumps(payload))
                with self.assertRaises(BuildProvenanceError) as cm:
                    self.verify_provenance(suite_dir)
                self.assertEqual(cm.exception.details["field"], field)

    def test_invalid_container_diagnostics_identify_the_entry_and_the_specific_field(self):
        suite_dir = self.root / "ownership-diagnostic"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        ownership = self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)
        baseline = ownership.read_text()
        for field in ("name", "image"):
            with self.subTest(field=field):
                payload = json.loads(baseline)
                del payload["owned_containers"][0][field]
                ownership.write_text(json.dumps(payload))
                with self.assertRaises(BuildProvenanceError) as cm:
                    self.verify_provenance(suite_dir)
                self.assertEqual(cm.exception.details["entry_index"], 0)
                self.assertEqual(cm.exception.details["field"], field)
                self.assertTrue(cm.exception.details["reason"])

    def test_removing_indexed_ownership_keeps_the_exported_run_failed_when_regraded(self):
        # Removing an indexed file makes this package corrupt as well as incomplete.
        def remove_ownership(root):
            next(root.glob("runs/*/cleanup-evidence/ownership.json")).unlink()
        code = self.run_executor(
            self.make_request(run_id="missing-ownership-report"), after_suite=remove_ownership,
        )
        self.assertEqual(code, 1)
        outcome = grade_run_package(self.output_dir / "missing-ownership-report")
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertTrue(any(f.code == "SUITE_INTEGRITY_VIOLATION" for f in outcome.findings))
        self.assertTrue(any(f.code == "TASK_EVIDENCE_MISSING_AT_RUNTIME" for f in outcome.findings))

    def test_post_run_provenance_fails_when_live_context_reports_another_gonka_sha(self):
        suite_dir = self.root / "suite-wrong-sha"
        self.write_live_context(suite_dir, runtime_sha=OTHER_SHA)
        self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)

        with self.assertRaises(ObservedVersionError) as cm:
            self.verify_provenance(suite_dir)
        self.assertIn(
            "Observed runtime version does not match the selected sources", str(cm.exception)
        )
        self.assertIn(OTHER_SHA, str(cm.exception))

    def test_post_run_provenance_fails_when_a_container_ran_an_unexpected_image(self):
        suite_dir = self.root / "suite-wrong-image"
        self.write_live_context(suite_dir, runtime_sha=GONKA_SHA)
        self.write_ownership(suite_dir, image_id=FOREIGN_IMAGE_ID)

        with self.assertRaises(BuildProvenanceError) as cm:
            self.verify_provenance(suite_dir)
        self.assertIn(
            "Containers are running images that this build did not produce", str(cm.exception)
        )
        self.assertIn("genesis-node", str(cm.exception))

    # -- F1: what each selected task owes --------------------------------
    def test_a_boundary_only_selection_owes_no_live_context_at_all(self):
        """A probe that never starts a chain cannot be asked what it deployed."""
        boundary_lock = make_lock(
            gonka_bundle_sha256=hashlib.sha256(self.gonka_bundle_bytes).hexdigest(),
            contracts_bundle_sha256=hashlib.sha256(self.contracts_bundle_bytes).hexdigest(),
            scenarios=(BOUNDARY_TASK_ID,),
        )
        requirements = self.evidence_requirements(lock=boundary_lock)

        self.assertEqual([r.task_id for r in requirements], [BOUNDARY_TASK_ID])
        self.assertFalse(requirements[0].requires_live_context)
        self.assertFalse(requirements[0].requires_deployment_evidence)

        suite_dir = self.root / "suite-boundary"
        suite_dir.mkdir(parents=True, exist_ok=True)
        findings = self.verify_provenance(suite_dir, lock=boundary_lock)

        self.assertEqual(findings["live_contexts"], [])
        self.assertEqual(findings["deployment"], [])
        self.assertEqual(findings["missing_evidence"], [])

    def test_a_native_task_that_left_no_live_context_is_recorded_as_missing(self):
        """Absence is a finding about that task, never a silent skip."""
        suite_dir = self.root / "suite-native-silent"
        self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)

        findings = self.verify_provenance(suite_dir)

        self.assertEqual(len(findings["missing_evidence"]), 1)
        missing = findings["missing_evidence"][0]
        expected_run_id = self.evidence_requirements()[0].run_id
        self.assertEqual(missing["task_id"], NATIVE_TASK_ID)
        self.assertEqual(missing["run_id"], expected_run_id)
        self.assertEqual(
            missing["expected_path"],
            f"runs/{expected_run_id}/evidence/{expected_run_id}/live-context.json",
        )
        self.assertEqual(findings["live_contexts"], [])

    def test_a_live_context_in_another_directory_does_not_answer_for_a_native_task(self):
        """One task's evidence may never stand in for another's."""
        suite_dir = self.root / "suite-foreign-evidence"
        self.write_ownership(suite_dir, image_id=CHAIN_IMAGE_ID)
        stray = suite_dir / "runs" / "some-other-run" / "live-context.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text(
            json.dumps(
                live_context_payload(
                    run_id="some-other-run", gonka_sha=GONKA_SHA, runtime_sha=GONKA_SHA
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        findings = self.verify_provenance(suite_dir)

        self.assertEqual(findings["live_contexts"], [])
        self.assertEqual(len(findings["missing_evidence"]), 1)
        self.assertEqual(findings["missing_evidence"][0]["task_id"], NATIVE_TASK_ID)

    # -- the single run verdict -------------------------------------------
    def run_result(self, run_dir: Path) -> dict:
        return self.read_json(run_dir / RUN_RESULT_FILENAME)

    def test_a_complete_run_is_graded_passed_and_records_its_verdict_beside_the_evidence(self):
        """``run`` returns the verdict of the whole package, not the suite's."""
        exit_code = self.run_executor(self.make_request())
        run_dir = next(iter(self.output_dir.iterdir()))

        result = self.run_result(run_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.PASSED)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["suite_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(result["build_status"], ManifestStatus.COMPLETE)
        self.assertEqual(result["lock_sha256"], self.lock.lock_sha256)
        self.assertEqual([f["code"] for f in result["findings"]], [])
        self.assertEqual([e["task_id"] for e in result["task_evidence"]], [NATIVE_TASK_ID])
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_SATISFIED)
        self.assertTrue(result["task_evidence"][0]["requires_live_context"])

    def test_a_green_suite_whose_native_task_left_no_evidence_is_not_graded_passed(self):
        """The suite's own success is necessary, never sufficient.

        The suite writes ``overall_status: PASSED`` and the executor is handed
        exit code 0, yet the selected native task recorded nothing about the
        runtime it observed. Both the online pass and the offline reader must
        notice, and the run must not exit 0.
        """
        exit_code = self.run_executor(self.make_request(), write_live_context=False)
        run_dir = next(iter(self.output_dir.iterdir()))

        result = self.run_result(run_dir)
        codes = [f["code"] for f in result["findings"]]

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.FAILED)
        self.assertEqual(result["suite_status"], ExecutionStatus.FAILED.value)
        self.assertIn("TASK_EVIDENCE_MISSING", codes)
        # Recorded while the run was still online, by the executor itself.
        self.assertIn("TASK_EVIDENCE_MISSING_AT_RUNTIME", codes)
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_MISSING)

    def test_a_boundary_only_run_passes_without_producing_any_live_context(self):
        """Review finding F1: a boundary probe owes no deployment evidence."""
        self.lock = make_lock(
            gonka_bundle_sha256=hashlib.sha256(self.gonka_bundle_bytes).hexdigest(),
            contracts_bundle_sha256=hashlib.sha256(self.contracts_bundle_bytes).hexdigest(),
            scenarios=(BOUNDARY_TASK_ID,),
        )
        self.lock_path = write_run_lock(
            self.lock, self.package_dir / "boundary.run.lock.json"
        )

        exit_code = self.run_executor(self.make_request())
        run_dir = next(iter(self.output_dir.iterdir()))

        result = self.run_result(run_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.PASSED)
        self.assertEqual([e["task_id"] for e in result["task_evidence"]], [BOUNDARY_TASK_ID])
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_NOT_APPLICABLE)
        self.assertFalse(result["task_evidence"][0]["requires_live_context"])
        self.assertFalse(result["task_evidence"][0]["requires_deployment_evidence"])
        self.assertEqual([f["code"] for f in result["findings"]], [])

    # -- handover context ------------------------------------------------
    def test_e2e_run_context_has_no_ops_a8_imports(self):
        tree = ast.parse(Path(context_module.__file__).read_text(encoding="utf-8"))
        imported: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(
                    node.level, 0, msg="context.py must not import from its own package"
                )
                if node.module == "__future__":
                    self.assertEqual([alias.name for alias in node.names], ["annotations"])
                    continue
                imported.append(node.module or "")
        for module in imported:
            self.assertIn(
                module, {"dataclasses", "pathlib", "typing"},
                msg=f"context.py imports a module outside its allowed dependencies: {module!r}",
            )

    def test_e2e_run_context_exposes_the_expected_sha_flags_for_the_harness(self):
        work_root = self.workspace_dir / "a8-work"
        harness_dir = self.runner_root / "harness/testermint"
        context = E2ERunContext(
            harness_script=self.runner_root / "scripts" / "acceptance_harness.py",
            gonka_requested_sha=GONKA_SHA,
            contracts_requested_sha=CONTRACTS_SHA,
            expected_gonka_sha=GONKA_SHA,
            expected_marketplace_sha=CONTRACTS_SHA,
            work_root=work_root,
            gradle_user_home=self.workspace_dir / "run-build" / "gradle-home",
            testermint_harness_dir=harness_dir,
            expected_runtime={"wasmd": "v0.54.2"},
        )

        extras = context.harness_argv_extras()

        self.assertIn("--expected-gonka-sha", extras)
        self.assertEqual(extras[extras.index("--expected-gonka-sha") + 1], GONKA_SHA)
        self.assertIn("--expected-marketplace-sha", extras)
        self.assertEqual(extras[extras.index("--expected-marketplace-sha") + 1], CONTRACTS_SHA)
        self.assertIn("--work-root", extras)
        self.assertEqual(extras[extras.index("--work-root") + 1], str(work_root))
        self.assertEqual(
            extras[extras.index("--gradle-user-home") + 1],
            str(self.workspace_dir / "run-build" / "gradle-home"),
        )
        self.assertIn("--testermint-harness-dir", extras)
        self.assertEqual(extras[extras.index("--testermint-harness-dir") + 1], str(harness_dir))
        self.assertIn("--expected-runtime", extras)
        self.assertEqual(extras[extras.index("--expected-runtime") + 1], "wasmd=v0.54.2")
        self.assertNotIn("--overlay-dir", extras)
        self.assertNotIn("--expected-gonka-prepared-sha", extras)
        self.assertNotIn("--expected-gonka-base-sha", extras)
        self.assertNotIn("--allowed-test-path", extras)
        self.assertIn("--evidence-model", extras)
        self.assertEqual(
            extras[extras.index("--evidence-model") + 1], EVIDENCE_MODEL_IMMUTABLE
        )

    def test_the_e2e_context_declares_the_e2e_evidence_model_in_its_record(self):
        """The recorded context, not only the argv, names the evidence model.

        ``context.py`` must stay free of ``forward_e2e.suite`` imports (see the test
        above), so it repeats the model literal instead of importing it. That
        duplication is only safe while the two literals agree, which is what is
        pinned here.
        """
        context = E2ERunContext(
            harness_script=self.runner_root / "scripts" / "acceptance_harness.py",
            gonka_requested_sha=GONKA_SHA,
            contracts_requested_sha=CONTRACTS_SHA,
        )

        self.assertEqual(E2ERunContext.E2E_EVIDENCE_MODEL, EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(context.evidence_model, EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(context.to_dict()["evidence_model"], EVIDENCE_MODEL_IMMUTABLE)


    # -- producer evidence layouts ---------------------------------------
    # The harness appends the run ID in the nested layout; runtime snapshots
    # can also use the direct layout. Both must remain discoverable, while
    # unrelated contexts must not satisfy a selected task's requirements.
    def test_the_producer_direct_evidence_layout_is_recognised_by_the_policy(self):
        exit_code = self.run_executor(
            self.make_request(), live_context_relpath=PRODUCER_LIVE_CONTEXT_RELPATH
        )
        run_dir = next(iter(self.output_dir.iterdir()))
        result = self.read_json(run_dir / RUN_RESULT_FILENAME)
        findings = {finding["code"]: finding for finding in result["findings"]}

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.PASSED)
        self.assertEqual(result["suite_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(result["task_evidence"][0]["task_id"], NATIVE_TASK_ID)
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_SATISFIED)
        self.assertNotIn("TASK_EVIDENCE_MISSING", findings)
        self.assertNotIn("UNCLAIMED_LIVE_CONTEXT", findings)

    def test_the_producer_nested_evidence_layout_is_recognised_by_the_policy(self):
        exit_code = self.run_executor(
            self.make_request(),
            live_context_relpath=lambda task_run_id: f"evidence/{task_run_id}/{LIVE_CONTEXT_FILENAME}",
        )
        run_dir = next(iter(self.output_dir.iterdir()))
        result = self.read_json(run_dir / RUN_RESULT_FILENAME)
        findings = {finding["code"]: finding for finding in result["findings"]}

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.PASSED)
        self.assertEqual(result["suite_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(result["task_evidence"][0]["task_id"], NATIVE_TASK_ID)
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_SATISFIED)
        self.assertNotIn("TASK_EVIDENCE_MISSING", findings)
        self.assertNotIn("UNCLAIMED_LIVE_CONTEXT", findings)

    def test_missing_live_context_reports_all_searched_candidate_paths(self):
        exit_code = self.run_executor(
            self.make_request(), write_live_context=False
        )
        run_dir = next(iter(self.output_dir.iterdir()))
        result = self.read_json(run_dir / RUN_RESULT_FILENAME)
        findings = {finding["code"]: finding for finding in result["findings"]}

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.FAILED)
        self.assertEqual(result["task_evidence"][0]["status"], EVIDENCE_MISSING)
        self.assertIn("TASK_EVIDENCE_MISSING", findings)

        task_evidence = result["task_evidence"][0]
        self.assertTrue(len(task_evidence["problems"]) > 0)
        problem = task_evidence["problems"][0]
        self.assertEqual(problem["code"], "LIVE_CONTEXT_MISSING")
        searched = problem.get("searched_paths", [])
        self.assertEqual(len(searched), 3)
        task_run_id = task_evidence["run_id"]
        expected_candidates = [
            str(Path("runs") / task_run_id / "evidence" / task_run_id / LIVE_CONTEXT_FILENAME),
            str(Path("runs") / task_run_id / "evidence" / LIVE_CONTEXT_FILENAME),
            str(Path("runs") / task_run_id / LIVE_CONTEXT_FILENAME),
        ]
        self.assertEqual(searched, expected_candidates)

    def test_unclaimed_live_context_in_unrelated_directory_is_flagged(self):
        rogue_relpath = str(Path("runs") / "rogue-task" / LIVE_CONTEXT_FILENAME)
        exit_code = self.run_executor(
            self.make_request(),
            live_context_relpath=PRODUCER_LIVE_CONTEXT_RELPATH,
            extra_live_contexts=[rogue_relpath],
        )
        run_dir = next(iter(self.output_dir.iterdir()))
        result = self.read_json(run_dir / RUN_RESULT_FILENAME)
        findings = {finding["code"]: finding for finding in result["findings"]}

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], RunStatus.PASSED)
        self.assertIn("UNCLAIMED_LIVE_CONTEXT", findings)
        self.assertIn(
            rogue_relpath,
            findings["UNCLAIMED_LIVE_CONTEXT"]["details"]["paths"],
        )

    def test_executor_passes_ready_ordered_tasks_to_suite_runner(self):
        self.lock = make_lock(
            gonka_bundle_sha256=hashlib.sha256(self.gonka_bundle_bytes).hexdigest(),
            contracts_bundle_sha256=hashlib.sha256(self.contracts_bundle_bytes).hexdigest(),
            scenarios=(NATIVE_TASK_ID, BOUNDARY_TASK_ID, "go-query-error-classification"),
        )
        requested = [NATIVE_TASK_ID, BOUNDARY_TASK_ID, "go-boundary"]
        tasks, profile, normalised = resolve_e2e_selection(scenarios=requested)
        # Use the normalized selection that the planner actually records.
        self.lock.selection = _selection_section(profile, normalised, tasks)
        self.lock_path = write_run_lock(self.lock, self.package_dir / "mixed.run.lock.json")
        self.assertEqual(
            self.lock.selection["requested_scenarios"],
            ["go-query-error-classification", BOUNDARY_TASK_ID, NATIVE_TASK_ID],
        )

        exit_code = self.run_executor(self.make_request())

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(self.suite_calls), 1)
        self.assertEqual(
            [(task.task_id, task.ordinal) for task in self.suite_calls[0]["tasks"]],
            [
                ("go-query-error-classification", 1),
                (BOUNDARY_TASK_ID, 2),
                (NATIVE_TASK_ID, 3),
            ],
        )

    def test_locked_task_limits_reach_the_suite_plan_without_being_reset_to_catalog_defaults(self):
        # Distinct non-default values expose a fake suite that resolves the
        # catalog again instead of consuming the executor's prepared tasks.
        expected = {
            "timeout_minutes": 7,
            "stage_timeout_seconds": 123,
            "gradle_timeout_minutes": 3,
        }
        self.lock.limits.update({
            "task_timeout_minutes": {NATIVE_TASK_ID: expected["timeout_minutes"]},
            "stage_timeout_seconds": {NATIVE_TASK_ID: expected["stage_timeout_seconds"]},
            "gradle_timeout_minutes": {NATIVE_TASK_ID: expected["gradle_timeout_minutes"]},
            "total_budget_minutes": expected["timeout_minutes"],
        })
        self.lock_path = write_run_lock(self.lock, self.package_dir / "custom-limits.run.lock.json")
        run_id = "locked-task-limits"

        exit_code = self.run_executor(self.make_request(run_id=run_id))

        self.assertEqual(exit_code, 0)
        supplied_task = self.suite_calls[0]["tasks"][0]
        plan = self.read_json(self.output_dir / run_id / "suite" / run_id / "suite-plan.json")
        self.assertEqual(len(plan["tasks"]), 1)
        self.assertEqual(plan["tasks"][0]["task_id"], NATIVE_TASK_ID)
        for field, value in expected.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(supplied_task, field), value)
                self.assertEqual(plan["tasks"][0][field], value)

    def test_executor_rejects_lock_with_wrong_catalog_order(self):
        bad_lock = make_baseline_lock(
            selection={
                "profile": None,
                "scenarios": ["lock-exact-e", "go-query-error-classification"],  # wrong order: native before boundary
                "requested_scenarios": ["lock-exact-e", "go-query-error-classification"],
                "ordered_by": "catalog",
                "proof_levels": {
                    "lock-exact-e": "NATIVE",
                    "go-query-error-classification": "GO_BOUNDARY",
                },
            }
        )
        req = self.make_request(lock=bad_lock)
        with self.assertRaises(IntegrityError) as cm:
            self.run_executor(req)
        self.assertIn("not ordered by the catalog", str(cm.exception))

    def test_execute_plan_writes_run_json_status_json_and_result_json_alongside_e2e_run_result(self):
        run_id = "run-canonical-files"
        exit_code = self.run_executor(self.make_request(run_id=run_id))
        self.assertEqual(exit_code, 0)

        run_dir = self.output_dir / run_id
        run_inputs = self.read_json(run_dir / "run.json")
        self.assertEqual(run_inputs["schema_version"], "e2e/run-inputs/1")
        self.assertEqual(run_inputs["run_id"], run_id)
        self.assertEqual(run_inputs["plan_id"], self.lock.plan_id)
        self.assertEqual(run_inputs["lock_sha256"], self.lock.lock_sha256)
        self.assertEqual(run_inputs["sources"]["gonka"]["commit_sha"], GONKA_SHA)
        self.assertEqual(run_inputs["sources"]["contracts"]["commit_sha"], CONTRACTS_SHA)

        status = self.read_json(run_dir / "status.json")
        self.assertEqual(status["schema_version"], "e2e/run-status/1")
        self.assertEqual(status["run_id"], run_id)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["planned_tasks"], [NATIVE_TASK_ID])
        self.assertEqual(status["completed_tasks"], [NATIVE_TASK_ID])
        self.assertEqual(status["pending_tasks"], [])
        self.assertIsNone(status["first_error"])
        self.assertEqual(status["export_status"], "COMPLETED")

        short_result = self.read_json(run_dir / "result.json")
        full_result = self.read_json(run_dir / RUN_RESULT_FILENAME)
        self.assertEqual(short_result, full_result)
        self.assertEqual(short_result["status"], RunStatus.PASSED)

    def test_task_events_publish_live_status_before_the_evidence_export(self):
        run_id = "run-live-task-progress"
        observed = []

        def during_suite(kwargs):
            callback = kwargs["progress_callback"]
            callback("started", NATIVE_TASK_ID)
            running = self.read_json(self.output_dir / run_id / "status.json")
            observed.append((running["current_task"], running["completed_tasks"], running["export_status"]))
            callback("completed", NATIVE_TASK_ID)
            completed = self.read_json(self.output_dir / run_id / "status.json")
            observed.append((completed["current_task"], completed["completed_tasks"], completed["export_status"]))

        self.run_executor(self.make_request(run_id=run_id), during_suite=during_suite)
        self.assertEqual(observed, [
            (NATIVE_TASK_ID, [], "IN_PROGRESS"),
            (None, [NATIVE_TASK_ID], "IN_PROGRESS"),
        ])

    def test_export_stays_in_progress_until_delivery_has_finished(self):
        from forward_e2e.execution.executor import export_and_record_delivery

        run_id = "run-export-progress"
        observed = []

        def inspecting_export(stage, destination, delivery, *, say):
            observed.append(self.read_json(stage / "status.json")["export_status"])
            observed.append(self.read_json(destination / "status.json")["export_status"])
            return export_and_record_delivery(stage, destination, delivery, say=say)

        with patch("forward_e2e.execution.executor.export_and_record_delivery", side_effect=inspecting_export):
            self.run_executor(self.make_request(run_id=run_id))
        self.assertEqual(observed, ["IN_PROGRESS", "IN_PROGRESS"])
        self.assertEqual(
            self.read_json(self.output_dir / run_id / "status.json")["export_status"],
            "COMPLETED",
        )

    def test_nonzero_suite_exit_still_measures_and_records_after_execution_source_immutability(self):
        run_id = "suite-fail-with-after-execution"
        with self.assertRaises(SuiteExecutionError):
            self.run_executor(self.make_request(run_id=run_id), suite_exit=1)

        run_dir = self.output_dir / run_id
        build = self.read_json(run_dir / BUILD_MANIFEST_FILENAME)
        exec_m = self.read_json(run_dir / EXECUTION_MANIFEST_FILENAME)
        self.assertEqual(build["source_immutability"]["after_execution_verdict"], "UNCHANGED")
        self.assertEqual(build["source_immutability"]["verdict"], "UNCHANGED")
        for role in ("gonka", "contracts"):
            self.assertIn("after_execution", build["source_immutability"]["roles"][role])
            self.assertIsNotNone(exec_m["source_immutability"]["after_execution"][role])
        self.assertEqual(exec_m["source_immutability"]["verdict"], "UNCHANGED")

        status = self.read_json(run_dir / "status.json")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["first_error"]["code"], "SUITE_EXECUTION_FAILED")
        self.assertEqual(status["export_status"], "COMPLETED")

        verdict = self.read_json(run_dir / "result.json")
        finding_codes = [f["code"] for f in verdict["findings"]]
        self.assertNotIn("SOURCE_IMMUTABILITY_INCOMPLETE", finding_codes)

    def test_pre_acquired_sources_bypass_bundle_materialisation_when_worktree_and_sha_match(self):
        gonka_wt = self.root / "pre_gonka"
        contracts_wt = self.root / "pre_contracts"
        gonka_wt.mkdir(parents=True)
        contracts_wt.mkdir(parents=True)

        class _PreSource:
            def __init__(self, worktree: Path, commit_sha: str):
                self.worktree = worktree
                self.commit_sha = commit_sha
                self.bundle_path = worktree

        acquirer = MagicMock()
        restored_gonka = restore_source(
            "gonka",
            self.lock,
            acquirer=acquirer,
            package_dir=self.package_dir,
            worktree_root=self.root / "unused_wt",
            allow_remote_refetch=False,
            emit=lambda _: None,
            pre_acquired={"gonka": _PreSource(gonka_wt, GONKA_SHA)},
        )
        self.assertEqual(restored_gonka.origin, "direct")
        self.assertEqual(restored_gonka.worktree, gonka_wt)
        acquirer.verify_stored_bundle.assert_not_called()
        acquirer.materialise.assert_not_called()

    def test_replaying_a_direct_run_lock_fetches_the_same_sha_without_a_bundle(self):
        self.lock.gonka["bundle_relpath"] = ""
        self.lock.gonka["bundle_sha256"] = ""
        self.lock.gonka["repo_url"] = "https://example.invalid/gonka.git"
        acquired = SimpleNamespace(
            worktree=self.root / "refetched-gonka",
            bundle_path=self.root / "scratch-objects",
            bundle_sha256="",
        )
        acquired.worktree.mkdir()
        acquirer = MagicMock()
        acquirer.acquire.return_value = acquired

        restored = restore_source(
            "gonka", self.lock, acquirer=acquirer,
            package_dir=self.package_dir, worktree_root=self.root / "replay-src",
            allow_remote_refetch=True, emit=lambda _: None,
        )
        self.assertEqual(restored.origin, "refetched")
        self.assertEqual(restored.commit_sha, GONKA_SHA)
        self.assertEqual(restored.worktree, acquired.worktree)
        self.assertEqual(acquirer.acquire.call_args.kwargs["create_bundle"], False)
        self.assertEqual(acquirer.acquire.call_args.args[0].commit_sha, GONKA_SHA)

    def test_refetch_failure_does_not_print_a_credential_bearing_recorded_url(self):
        """Malformed lock URLs must not put token text in CLI diagnostics.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        self.lock.gonka["bundle_relpath"] = ""
        self.lock.gonka["bundle_sha256"] = ""
        self.lock.gonka["repo_url"] = "https://example.invalid/gonka.git?token=synthetic-secret"
        acquirer = MagicMock()
        acquirer.acquire.side_effect = ValueError("remote rejected")

        with self.assertRaises(SourceAcquisitionError) as caught:
            restore_source(
                "gonka", self.lock, acquirer=acquirer,
                package_dir=self.package_dir, worktree_root=self.root / "refetch-secret",
                allow_remote_refetch=True, emit=lambda _: None,
            )
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))


def _real_acquirer(runner: RecordingGitRunner, *, package_dir: Path, scratch_dir: Path):
    """A real SourceAcquirer whose git process runner is the recording fake."""
    from forward_e2e.execution.sources import SourceAcquirer

    return SourceAcquirer(
        GitClient(runner=runner),
        package_dir=Path(package_dir),
        scratch_dir=Path(scratch_dir),
    )


if __name__ == "__main__":
    unittest.main()
