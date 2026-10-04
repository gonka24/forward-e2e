"""Durable run package loading, verdict grading, and export lifecycle tests.

What these tests pin down
-------------------------
**F3 -- there was no single final verdict.** ``run`` returned the suite's exit
code, ``report`` read only the nested suite, and the build manifest recorded a
third opinion, so the same run could exit non-zero and be reported ``PASSED``
afterwards. The fix is :mod:`forward_e2e.execution.outcome` (``RunStatus``, ``Finding``,
``RunOutcome``, ``evaluate_run``, ``recompute_status``) plus
``executor.grade_run_package``, which writes one ``e2e-run-result.json``.
Proven here: the four final states are produced by the right combinations of
findings, are distinguishable in that document, and map to the documented exit
codes -- and, as the negative of each, an INCOMPLETE run is never reported
FAILED and a CANCELLED run is never reported PASSED or FAILED even though its
package also carries failure-coded findings.

**F4 -- run artefacts were scattered and a partial run left nothing durable.**
The fix is :mod:`forward_e2e.execution.runpackage`: a durable ``<workspace>/<run-id>/run/``
staging directory written *before* the long stages, ``export_run_package``
called on the success *and* the failure path, a strict ``LoadedRunPackage``
reader, ``EXPORT_EXCLUDED_NAMES`` so the derived verdict is never exported, and
the offline re-derivation in ``provenance_findings``. ``cli.cmd_report`` and
``cli.cmd_recover`` resolve upward from a nested suite to the package.

How the fixtures are built
--------------------------
The positive fixtures are produced by running the real ``execute_plan`` with
mocks *only* at the external boundaries it cannot reach offline: the Docker
image lookup, the Git source acquisition, the Git tree preparation, the runner
image layout and the suite orchestrator itself. Everything the two findings are
about -- the durable staging, the manifests, the export, the reader, the
verdict, ``report`` and ``recover`` -- is the production code. The stand-in
suite runner writes the real suite layout directly to the durable package
at ``<stage>/suite/<run-id>``, using the recorded ``live-context.json`` from
``tests/fixtures/evidence`` through :mod:`tests.unit.runner.real_fixtures`.

Every negative fixture changes or deletes exactly ONE mandatory fact of a
positive one, and when a manifest is changed it is re-sealed the way the
producer seals it (``execution-manifest.build_manifest_sha256``), so the
negative proves the intended defect instead of accidentally proving tampering.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from forward_e2e.execution import runpackage
from forward_e2e.execution.builder import verify_running_images
from forward_e2e.execution.cancel import Cancellation
from forward_e2e.execution.cli import cmd_recover, cmd_report, parse_e2e_args
from forward_e2e.execution.compat import ExpectationKind
from forward_e2e.execution.errors import BuildProvenanceError, ExecutionCancelled
from forward_e2e.execution.executor import (
    ExecutionRequest,
    execute_plan,
    grade_run_package,
    suite_export_dir,
)
from forward_e2e.execution.outcome import (
    BLOCKING,
    RUN_RESULT_FILENAME,
    RunStatus,
    evaluate_run,
)
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_FILENAME,
    DELIVERY_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    RUN_LOCK_FILENAME,
    BuildManifest,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    content_sha256,
    write_run_lock,
)
from forward_e2e.execution.runner_image import RunnerImageIdentity
from forward_e2e.execution.runpackage import (
    BUILD_OUTPUT_DIRNAME,
    EXPORT_EXCLUDED_NAMES,
    REQUIRED_DOCUMENTS,
    ExportConflict,
    LoadedRunPackage,
    export_run_package,
    run_stage_dir,
    sha256_file,
)

from forward_e2e.execution.sources import bundle_ref
from forward_e2e.suite.models import (
    EvidenceStatus,
    ExecutionStatus,
    make_task_run_id,
)
from tests.unit.runner.real_fixtures import (
    GONKA_SOURCE_SHA,
    GONKA_TREE_SHA,
    MARKETPLACE_SHA,
    MARKETPLACE_TREE_SHA,
)
from tests.unit.runner.support.fakes import (
    FakeAcquirer,
    StubLayout,
    fake_process_runner,
    make_test_adapter,
    snapshot_fingerprint,
    write_suite_output,
)
from tests.unit.runner.support.packages import make_baseline_lock

#: The one task every fixture selects. It is a real catalog task, so the
#: evidence policy derives its real proof level and expected artefacts.
SELECTED_TASK_ID = "lock-exact-e"

#: The lock pins the upstream requested commit. In the immutable-source model,
#: the running binary is built directly from and reports that exact commit.
REQUESTED_SHA = GONKA_SOURCE_SHA
CONTRACTS_SHA = MARKETPLACE_SHA

RUNNER_IMAGE_ID = "sha256:" + "a" * 64
#: An image id no synthetic build ever produced: used only where a *foreign*
#: container has to be recognised as foreign.
FOREIGN_IMAGE_ID = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# fixtures: the documents the real producers write
# ---------------------------------------------------------------------------
def make_lock(
    gonka_bundle_sha256: str = hashlib.sha256(b"GIT-BUNDLE-SYNTHETIC-GONKA").hexdigest(),
    contracts_bundle_sha256: str = hashlib.sha256(b"GIT-BUNDLE-SYNTHETIC-CONTRACTS").hexdigest(),
) -> RunLock:
    """A complete lock with the selection section the planner really writes."""
    return make_baseline_lock(
        scenarios=[SELECTED_TASK_ID],
        plan_id="plan-r11-run-package",
        created_at_utc="2026-09-11T11:00:00+00:00",
        gonka_sha=REQUESTED_SHA,
        contracts_sha=CONTRACTS_SHA,
        gonka_bundle_sha256=gonka_bundle_sha256,
        contracts_bundle_sha256=contracts_bundle_sha256,
        gonka_bundle_ref=bundle_ref("gonka"),
        contracts_bundle_ref=bundle_ref("contracts"),
    )


def write_staged_suite(
    suite_dir: Path,
    suite_id: str,
    *,
    overall_status: ExecutionStatus = ExecutionStatus.PASSED,
    ownership_image: str | None = None,
) -> Path:
    """Write one A8 suite using the shared suite output builder."""
    passed = overall_status is ExecutionStatus.PASSED
    return write_suite_output(
        suite_dir=suite_dir,
        suite_id=suite_id,
        scenarios=[SELECTED_TASK_ID],
        execution_status=overall_status,
        evidence_status=EvidenceStatus.COMPLETE if passed else EvidenceStatus.INCOMPLETE,
        ownership_image=ownership_image,
    )


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reseal_build_manifest(package_root: Path, mutate) -> BuildManifest:
    """Change one recorded fact and re-seal the package the way the run does.

    The executor writes the build manifest and *then* records its hash in the
    execution manifest. A test that changed the manifest without repeating that
    step would only ever prove ``BUILD_MANIFEST_MODIFIED``, which is a different
    finding than the one under test.
    """
    package_root = Path(package_root)
    build_path = package_root / BUILD_MANIFEST_FILENAME
    manifest = BuildManifest.from_dict(read_json(build_path))
    mutate(manifest)
    manifest.write(build_path)
    execution_path = package_root / EXECUTION_MANIFEST_FILENAME
    execution = ExecutionManifest.from_dict(read_json(execution_path))
    execution.build_manifest_sha256 = content_sha256(manifest.to_dict())
    execution.write(execution_path)
    return manifest


def finding_codes(outcome) -> list:
    return [finding.code for finding in outcome.findings]


# ---------------------------------------------------------------------------
# the shared execution harness
# ---------------------------------------------------------------------------
class RunPackageCase(unittest.TestCase):
    """Drives the real ``execute_plan`` with only its boundaries replaced."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r11pkg-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.package_dir = self.root / "package"
        self.output_dir = self.root / "out"
        self.workspace_dir = self.root / "workspace"
        self.runner_root = self.root / "runner"
        (self.package_dir / "bundles").mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        self.workspace_dir.mkdir(parents=True)
        (self.runner_root / "scripts").mkdir(parents=True)
        (self.runner_root / "ops" / "a8" / "harness" / "testermint").mkdir(parents=True)
        for role in ("gonka", "contracts"):
            (self.package_dir / "bundles" / f"{role}.bundle").write_bytes(
                f"GIT-BUNDLE-SYNTHETIC-{role.upper()}".encode("utf-8")
            )
        self.lock = make_lock()
        self.lock_path = write_run_lock(self.lock, self.package_dir / RUN_LOCK_FILENAME)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers ---------------------------------------------------------
    def observed_image(self) -> RunnerImageIdentity:
        return RunnerImageIdentity(
            locator="forward-e2e-runner:local",
            image_id=RUNNER_IMAGE_ID,
            repo_digest=None,
            locator_is_immutable=False,
            portability="local-image-only",
            resolved_by="launcher-injected",
        )

    def stage_dir(self, run_id: str) -> Path:
        return run_stage_dir(self.workspace_dir, run_id)

    def run_dir(self, run_id: str) -> Path:
        return self.output_dir / run_id

    def execute(
        self,
        run_id: str,
        *,
        suite_status: ExecutionStatus = ExecutionStatus.PASSED,
        ownership_image: str | None = RUNNER_IMAGE_ID,
        on_suite=None,
        on_prepare=None,
        cancel_signal: int | None = None,
    ):
        """Run one full execution and report what came out of it.

        ``on_prepare`` observes the durable state before source measurement
        and building. ``on_suite`` observes it after building, before the suite.
        """
        messages: list = []
        suite_calls: list = []
        cancellation = Cancellation() if cancel_signal is not None else None
        prepared_seen = False

        def suite_runner(**kwargs):
            suite_calls.append(kwargs)
            suite_id = str(kwargs["suite_id"])
            if on_suite is not None:
                on_suite(kwargs)
            exported = Path(kwargs["output_dir"]) / suite_id
            write_staged_suite(
                exported,
                suite_id,
                overall_status=suite_status,
                ownership_image=ownership_image,
            )
            return 0 if suite_status is ExecutionStatus.PASSED else 1

        def capture(worktree: Path) -> dict:
            nonlocal prepared_seen
            if not prepared_seen:
                prepared_seen = True
                if on_prepare is not None:
                    on_prepare({"worktree": worktree, "requested_sha": REQUESTED_SHA})
                if cancellation is not None:
                    # The operator interrupts after the sources are restored and
                    # before the first expensive stage, which is where the executor
                    # checks the token.
                    cancellation.request(cancel_signal, emit=messages.append)
            if Path(worktree).name == "gonka":
                return snapshot_fingerprint(REQUESTED_SHA, GONKA_TREE_SHA)
            return snapshot_fingerprint(CONTRACTS_SHA, MARKETPLACE_TREE_SHA)

        request = ExecutionRequest(
            lock=self.lock,
            lock_path=self.lock_path,
            package_dir=self.package_dir,
            output_dir=self.output_dir,
            workspace_dir=self.workspace_dir,
            run_id=run_id,
            cancellation=cancellation,
        )

        git = MagicMock(name="git-client")
        git.stdout.return_value = REQUESTED_SHA[:12]

        exit_code = None
        error = None
        with patch.dict(os.environ, {}, clear=False), patch(
            "forward_e2e.execution.executor.RunnerLayout", StubLayout
        ), patch(
            "forward_e2e.execution.executor.resolve_runner_image", return_value=self.observed_image()
        ), patch(
            "forward_e2e.execution.executor.assert_runner_matches_lock", return_value=None
        ), patch(
            "forward_e2e.execution.executor.rebuild_adapter",
            side_effect=lambda role, lock, layout: make_test_adapter(
                role, with_runtime=role == "gonka", with_build=True
            ),
        ), patch(
            "forward_e2e.execution.executor.SourceAcquirer", FakeAcquirer
        ):
            try:
                exit_code = execute_plan(
                    request,
                    git=git,
                    build_runner=fake_process_runner,
                    docker_runner=fake_process_runner,
                    runner_root=self.runner_root,
                    env={},
                    emit=messages.append,
                    suite_runner=suite_runner,
                    snapshot_capture=capture,
                )
            except Exception as exc:  # the failure path is part of what is tested
                error = exc

        return SimpleNamespace(
            run_id=run_id,
            exit_code=exit_code,
            error=error,
            messages=messages,
            suite_calls=suite_calls,
            stage_dir=self.stage_dir(run_id),
            run_dir=self.run_dir(run_id),
            verdict_path=self.run_dir(run_id) / RUN_RESULT_FILENAME,
        )

    # -- named fixtures --------------------------------------------------
    def passing_run(self, run_id: str = "e2e-r11-passed"):
        """A complete run whose suite passed and whose provenance re-verified."""
        outcome = self.execute(run_id)
        self.assertIsNone(
            outcome.error, msg=f"the passing fixture must not fail: {outcome.error}"
        )
        self.assertEqual(outcome.exit_code, 0, msg="the positive fixture must exit successfully")
        self.assertEqual(read_json(outcome.verdict_path)["status"], RunStatus.PASSED)
        return outcome

    def provenance_failure_run(self, run_id: str = "e2e-r11-failed"):
        """Suite PASSED, then the post-run provenance rejects the run.

        The suite leaves ownership evidence naming a container image this build
        never produced, which is exactly the F3 scenario: a locally successful
        suite inside a run that failed.
        """
        outcome = self.execute(run_id, ownership_image=FOREIGN_IMAGE_ID)
        self.assertIsInstance(outcome.error, BuildProvenanceError)
        return outcome

    def incomplete_snapshot(self, run_id: str = "e2e-r11-incomplete") -> Path:
        """Snapshot after the build, before the suite writes any artifacts."""
        snapshot = self.root / "snapshots" / run_id
        snapshot.parent.mkdir(parents=True, exist_ok=True)

        def take_snapshot(_kwargs):
            shutil.copytree(self.stage_dir(run_id), snapshot)

        self.execute(run_id, on_suite=take_snapshot)
        return snapshot

    def cancelled_run(self, run_id: str, signum: int):
        outcome = self.execute(run_id, cancel_signal=signum)
        self.assertIsInstance(outcome.error, ExecutionCancelled)
        return outcome


# ---------------------------------------------------------------------------
# 1. the truth table of final states
# ---------------------------------------------------------------------------
class FinalVerdictTruthTableTests(RunPackageCase):
    """F3: four states, four exit codes, one document, no confusion between them."""

    def test_a_complete_run_whose_suite_passed_and_provenance_reverified_is_passed_with_exit_zero(self):
        outcome = self.passing_run()
        verdict = read_json(outcome.verdict_path)
        self.assertEqual(verdict["status"], RunStatus.PASSED)
        self.assertEqual(verdict["exit_code"], 0)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(verdict["suite_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(verdict["build_status"], ManifestStatus.COMPLETE)
        self.assertEqual(
            [f for f in verdict["findings"] if f["severity"] == BLOCKING],
            [],
            msg="a PASSED run may not carry a blocking finding",
        )

    def test_a_run_whose_suite_passed_but_whose_containers_were_foreign_is_failed_with_exit_one(self):
        outcome = self.provenance_failure_run()
        verdict = read_json(outcome.verdict_path)
        # The suite's own answer is still PASSED: that is the whole point.
        suite_result = read_json(
            suite_export_dir(outcome.run_dir, outcome.run_id) / "suite-result.json"
        )
        self.assertEqual(suite_result["overall_status"], ExecutionStatus.PASSED.value)
        self.assertEqual(verdict["status"], RunStatus.FAILED)
        self.assertEqual(verdict["exit_code"], 1)
        codes = {f["code"] for f in verdict["findings"]}
        self.assertIn("BUILD_PROVENANCE_FAILED", codes)

    def test_a_snapshot_before_any_suite_artifacts_exist_is_incomplete_and_is_never_reported_failed(self):
        snapshot = self.incomplete_snapshot()
        outcome = grade_run_package(snapshot)
        self.assertEqual(outcome.status, RunStatus.INCOMPLETE)
        self.assertEqual(outcome.exit_code, 1)
        self.assertNotEqual(outcome.status, RunStatus.FAILED)
        # Every blocking reason is an incompleteness, not a verdict: an
        # unfinished run must not be dressed up as a failed one.
        self.assertTrue(outcome.blocking)
        for finding in outcome.blocking:
            self.assertTrue(
                finding.code.startswith("INCOMPLETE_") or finding.code == "DELIVERY_MANIFEST_MISSING",
                msg=f"{finding.code} is not an incompleteness",
            )
        self.assertIn("INCOMPLETE_BUILD_MANIFEST_UNSEALED", finding_codes(outcome))
        self.assertIn("INCOMPLETE_SUITE_MISSING", finding_codes(outcome))

    def test_an_incomplete_run_that_also_really_failed_is_reported_failed_rather_than_incomplete(self):
        """The negative of the previous test: incompleteness must not soften a failure."""
        snapshot = self.incomplete_snapshot("e2e-r11-incomplete-then-failed")
        outcome = grade_run_package(
            snapshot,
            failure=BuildProvenanceError(
                "The prepared runtime does not descend from the requested commit",
                {"requested": REQUESTED_SHA},
            ),
        )
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.exit_code, 1)
        self.assertIn("BUILD_PROVENANCE_FAILED", finding_codes(outcome))
        self.assertIn("INCOMPLETE_SUITE_MISSING", finding_codes(outcome))

    def test_a_cancelled_run_keeps_the_cancellations_own_exit_code_and_is_never_failed(self):
        outcome = self.cancelled_run("e2e-r11-cancelled-int", signal.SIGINT)
        # A cancelled run is exported like any other: the operator must be able
        # to see what was already proven before the stop.
        self.assertTrue(outcome.verdict_path.is_file())
        verdict = read_json(outcome.verdict_path)
        self.assertEqual(verdict["status"], RunStatus.CANCELLED)
        self.assertEqual(verdict["exit_code"], 130)
        self.assertEqual(verdict["cancellation_exit_code"], 130)
        codes = {f["code"] for f in verdict["findings"]}
        self.assertIn("RUN_CANCELLED", codes)
        # The package also carries genuinely failure-coded findings; the
        # interruption still wins, because a stopped run says nothing about the
        # sources.
        self.assertTrue({"BUILD_NOT_COMPLETE", "EXECUTION_CANCELLED"} & codes)
        self.assertNotEqual(verdict["status"], RunStatus.FAILED)
        self.assertNotEqual(verdict["status"], RunStatus.PASSED)

    def test_a_run_cancelled_by_sigterm_reports_the_sigterm_exit_code(self):
        outcome = self.cancelled_run("e2e-r11-cancelled-term", signal.SIGTERM)
        verdict = read_json(outcome.verdict_path)
        self.assertEqual(verdict["status"], RunStatus.CANCELLED)
        self.assertEqual(verdict["exit_code"], 143)

    def test_a_cancellation_recorded_without_an_exit_code_falls_back_to_sigterm(self):
        """One fact removed: the recorded cancellation loses its exit code."""
        outcome = self.cancelled_run("e2e-r11-cancelled-noexit", signal.SIGINT)
        reseal_build_manifest(
            outcome.run_dir,
            lambda manifest: manifest.cancellation.pop("exit_code", None),
        )
        regraded = grade_run_package(outcome.run_dir)
        self.assertEqual(regraded.status, RunStatus.CANCELLED)
        self.assertEqual(regraded.exit_code, 143)

    def test_the_four_final_states_are_distinguishable_in_the_written_run_results(self):
        passed = self.passing_run("e2e-r11-table-passed")
        failed = self.provenance_failure_run("e2e-r11-table-failed")
        cancelled = self.cancelled_run("e2e-r11-table-cancelled", signal.SIGTERM)
        incomplete_root = self.incomplete_snapshot("e2e-r11-table-incomplete")
        grade_run_package(incomplete_root)

        documents = {
            "passed": read_json(passed.verdict_path),
            "failed": read_json(failed.verdict_path),
            "cancelled": read_json(cancelled.verdict_path),
            "incomplete": read_json(incomplete_root / RUN_RESULT_FILENAME),
        }
        self.assertEqual(
            {name: doc["status"] for name, doc in documents.items()},
            {
                "passed": RunStatus.PASSED,
                "failed": RunStatus.FAILED,
                "cancelled": RunStatus.CANCELLED,
                "incomplete": RunStatus.INCOMPLETE,
            },
        )
        self.assertEqual(
            {name: doc["exit_code"] for name, doc in documents.items()},
            {"passed": 0, "failed": 1, "cancelled": 143, "incomplete": 1},
        )
        self.assertEqual(len({doc["status"] for doc in documents.values()}), 4)
        # Only one of the four is a statement that the sources are good.
        self.assertEqual(
            [name for name, doc in documents.items() if doc["exit_code"] == 0], ["passed"]
        )

    def test_the_summary_the_operator_reads_agrees_with_the_written_document(self):
        outcome = self.provenance_failure_run("e2e-r11-summary-agreement")
        verdict = read_json(outcome.verdict_path)
        banner = f"E2E run {outcome.run_id}: {verdict['status']} (exit {verdict['exit_code']})"
        self.assertIn(banner, outcome.messages)


# ---------------------------------------------------------------------------
# 2. a failing run still leaves a durable, complete package
# ---------------------------------------------------------------------------
class DurableRunPackageOnFailureTests(RunPackageCase):
    """F4: the durable copy exists before the long stages and survives failure."""

    def test_the_lock_and_both_manifests_are_durable_before_the_long_stages_begin(self):
        seen: dict = {}

        def inspect(_kwargs):
            stage = self.stage_dir("e2e-r11-durable-early")
            seen["present"] = sorted(
                name for name in REQUIRED_DOCUMENTS if (stage / name).is_file()
            )
            seen["build_status"] = read_json(stage / BUILD_MANIFEST_FILENAME)["status"]
            seen["lock_bytes"] = (stage / RUN_LOCK_FILENAME).read_bytes()

        outcome = self.execute("e2e-r11-durable-early", on_prepare=inspect)
        self.assertIsNone(outcome.error)
        self.assertEqual(seen["present"], sorted(REQUIRED_DOCUMENTS))
        self.assertEqual(seen["lock_bytes"], self.lock_path.read_bytes())
        self.assertEqual(seen["build_status"], ManifestStatus.IN_PROGRESS)

    def test_a_failing_run_still_exports_a_complete_package_to_the_host_output(self):
        outcome = self.provenance_failure_run("e2e-r11-failure-export")
        for name in REQUIRED_DOCUMENTS:
            self.assertTrue(
                (outcome.run_dir / name).is_file(),
                msg=f"{name} is missing from the exported package of a failed run",
            )
            self.assertEqual(
                sha256_file(outcome.run_dir / name),
                sha256_file(outcome.stage_dir / name),
                msg=f"the exported {name} is not the durable one",
            )
        self.assertTrue(suite_export_dir(outcome.run_dir, outcome.run_id).is_dir())
        self.assertTrue((outcome.run_dir / BUILD_OUTPUT_DIRNAME).is_dir())
        self.assertTrue(outcome.verdict_path.is_file())
        self.assertEqual(
            read_json(outcome.run_dir / BUILD_MANIFEST_FILENAME)["status"],
            ManifestStatus.FAILED,
        )

    def test_the_original_failure_is_re_raised_after_the_verdict_has_been_written(self):
        """The negative: grading a failure must not swallow it."""
        outcome = self.provenance_failure_run("e2e-r11-failure-reraised")
        self.assertIsInstance(outcome.error, BuildProvenanceError)
        self.assertIsNone(
            outcome.exit_code, msg="execute_plan returned instead of re-raising"
        )
        self.assertIn("Containers are running images", str(outcome.error))
        # The verdict was written *before* the exception left execute_plan.
        self.assertTrue(outcome.verdict_path.is_file())
        self.assertEqual(read_json(outcome.verdict_path)["status"], RunStatus.FAILED)

    def test_when_the_host_export_fails_the_durable_stage_still_holds_the_graded_verdict(self):
        run_id = "e2e-r11-export-blocked"

        def occupy_destination(_kwargs):
            # Somebody else's bytes appear at the destination while the run is
            # still going: the export can no longer complete.
            destination = self.run_dir(run_id)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / BUILD_MANIFEST_FILENAME).write_text(
                '{"schema_version": "not-this-run"}\n', encoding="utf-8"
            )

        outcome = self.execute(run_id, on_suite=occupy_destination)
        self.assertIsNone(outcome.error)
        self.assertTrue(
            any("could not be exported" in message for message in outcome.messages),
            msg="the failed export was not reported to the operator",
        )
        # The durable copy is intact and carries the verdict, so `recover` has
        # something to export later.
        self.assertTrue((outcome.stage_dir / RUN_RESULT_FILENAME).is_file())
        for name in REQUIRED_DOCUMENTS:
            self.assertTrue((outcome.stage_dir / name).is_file())
        delivery_doc = read_json(outcome.stage_dir / DELIVERY_MANIFEST_FILENAME)
        self.assertEqual(delivery_doc["status"], "FAILED")
        self.assertEqual(delivery_doc["attempts"][-1]["status"], "FAILED")
        exec_notes = read_json(outcome.stage_dir / EXECUTION_MANIFEST_FILENAME)["notes"]
        self.assertNotIn("export failed", " ".join(exec_notes))

    def test_the_durable_stage_is_the_documented_directory_next_to_the_raw_suite(self):
        outcome = self.passing_run("e2e-r11-durable-layout")
        self.assertEqual(outcome.stage_dir, self.workspace_dir / outcome.run_id / "run")
        self.assertTrue((outcome.stage_dir / BUILD_OUTPUT_DIRNAME).is_dir())
        # Suite evidence is collected directly into the durable run package stage
        # without creating an intermediate staged suite copy on the workspace volume.
        self.assertTrue(suite_export_dir(outcome.stage_dir, outcome.run_id).is_dir())
        self.assertFalse(
            (self.workspace_dir / outcome.run_id / "suite" / "suites" / outcome.run_id).exists()
        )


# ---------------------------------------------------------------------------
# 3. the derived verdict is never exported
# ---------------------------------------------------------------------------
class ExportPathDisjointnessTests(unittest.TestCase):
    def test_export_rejects_equal_or_nested_source_and_destination_before_mutation(self):
        with tempfile.TemporaryDirectory(prefix="a8-export-overlap-") as tmp:
            root = Path(tmp)
            stage = root / "stage"
            stage.mkdir()
            (stage / "source.txt").write_text("durable", encoding="utf-8")
            nested_source = stage / "nested-source"
            nested_source.mkdir()
            cases = (
                (stage, stage),
                (stage, root),
                (stage, stage / "destination-child"),
                (nested_source, stage),
            )

            for source, destination in cases:
                with self.subTest(source=source, destination=destination):
                    with self.assertRaises(ExportConflict):
                        export_run_package(source, destination)
            self.assertEqual((stage / "source.txt").read_text(encoding="utf-8"), "durable")
            self.assertFalse((stage / "destination-child").exists())


class ExportExcludesTheDerivedVerdictTests(RunPackageCase):
    """F4: re-grading a recovered package may not look like tampering."""

    def test_export_excludes_only_root_transport_documents(self):
        stage = self.root / "stage"
        stage.mkdir()
        destination = self.root / "exported"
        nested = stage / "suite" / "run-1"
        nested.mkdir(parents=True)
        for name in EXPORT_EXCLUDED_NAMES:
            (stage / name).write_bytes(b"root transport document")
            (nested / name).write_bytes(b"indexed suite evidence")

        report = export_run_package(stage, destination)

        for name in EXPORT_EXCLUDED_NAMES:
            relative = f"suite/run-1/{name}"
            self.assertFalse((destination / name).exists())
            self.assertEqual((destination / relative).read_bytes(), b"indexed suite evidence")
            self.assertIn(relative, report.copied)

    def test_the_exported_package_never_contains_the_derived_verdict_document(self):
        outcome = self.passing_run("e2e-r11-export-exclusion")
        # `report --run <durable stage>` grades the stage, which puts a verdict
        # next to the durable documents.
        grade_run_package(outcome.stage_dir)
        self.assertTrue((outcome.stage_dir / RUN_RESULT_FILENAME).is_file())

        report = export_run_package(outcome.stage_dir, outcome.run_dir)
        exported_names = {Path(p).name for p in report.copied + report.identical}
        self.assertNotIn(RUN_RESULT_FILENAME, exported_names)
        self.assertIn(RUN_RESULT_FILENAME, EXPORT_EXCLUDED_NAMES)
        self.assertEqual(report.conflicts, [])

    def test_re_exporting_the_same_stage_after_grading_is_idempotent(self):
        outcome = self.passing_run("e2e-r11-export-idempotent")
        grade_run_package(outcome.stage_dir)
        verdict_before = outcome.verdict_path.read_bytes()

        report = export_run_package(outcome.stage_dir, outcome.run_dir)

        self.assertEqual(report.copied, [], msg="a repeat export copied something new")
        self.assertIn(RUN_LOCK_FILENAME, report.identical)
        self.assertIn(BUILD_MANIFEST_FILENAME, report.identical)
        self.assertIn(EXECUTION_MANIFEST_FILENAME, report.identical)
        # The verdict already in the destination is the destination's own; the
        # stage's copy neither replaced it nor conflicted with it.
        self.assertEqual(outcome.verdict_path.read_bytes(), verdict_before)

    def test_a_destination_file_with_different_bytes_at_the_same_path_is_a_conflict(self):
        """The negative: idempotence must not degrade into silent overwriting."""
        outcome = self.passing_run("e2e-r11-export-conflict")
        foreign = b'{"schema_version": "e2e/build-manifest/1", "run_id": "someone-else"}\n'
        (outcome.run_dir / BUILD_MANIFEST_FILENAME).write_bytes(foreign)

        with self.assertRaises(ExportConflict) as caught:
            export_run_package(outcome.stage_dir, outcome.run_dir)

        conflicts = caught.exception.details["conflicts"]
        self.assertEqual([entry["path"] for entry in conflicts], [BUILD_MANIFEST_FILENAME])
        self.assertEqual(
            (outcome.run_dir / BUILD_MANIFEST_FILENAME).read_bytes(),
            foreign,
            msg="the conflicting destination file was overwritten anyway",
        )

    def test_a_recovered_package_can_be_regraded_without_an_export_conflict(self):
        """The end-to-end form of the same claim, across two locations."""
        outcome = self.passing_run("e2e-r11-regrade-after-export")
        grade_run_package(outcome.stage_dir)
        elsewhere = self.root / "relocated" / outcome.run_id

        export_run_package(outcome.stage_dir, elsewhere)
        self.assertFalse((elsewhere / RUN_RESULT_FILENAME).exists())
        if (outcome.stage_dir / DELIVERY_MANIFEST_FILENAME).is_file():
            shutil.copy2(
                outcome.stage_dir / DELIVERY_MANIFEST_FILENAME,
                elsewhere / DELIVERY_MANIFEST_FILENAME,
            )

        regraded = grade_run_package(elsewhere)
        self.assertEqual(regraded.status, RunStatus.PASSED)
        self.assertTrue((elsewhere / RUN_RESULT_FILENAME).is_file())
        # And exporting once more on top of the re-graded copy is still safe.
        again = export_run_package(outcome.stage_dir, elsewhere)
        self.assertEqual(again.conflicts, [])


# ---------------------------------------------------------------------------
# 4. the strict reader
# ---------------------------------------------------------------------------
class StrictRunPackageReaderTests(RunPackageCase):
    """F3/F4: a missing document is an incomplete run, never a partial object."""

    def test_malformed_context_evidence_model_yields_blocking_finding(self):
        outcome = self.passing_run("malformed-context-model")
        suite = suite_export_dir(outcome.run_dir, outcome.run_id)
        context_path = suite / "e2e-context.json"
        context_path.write_text(
            json.dumps({"evidence_model": ["a8/immutable-sources/1"]}), encoding="utf-8"
        )

        loaded = LoadedRunPackage.load(outcome.run_dir)

        self.assertIn("EVIDENCE_MODEL_INVALID", {f.code for f in loaded.load_findings()})
        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertEqual(read_json(outcome.verdict_path)["status"], RunStatus.FAILED)

    def test_malformed_suite_plan_schema_yields_blocking_finding(self):
        outcome = self.passing_run("malformed-plan-schema")
        suite = suite_export_dir(outcome.run_dir, outcome.run_id)
        plan_path = suite / "suite-plan.json"
        plan = read_json(plan_path)
        plan["schema_version"] = ["1.0.0"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        loaded = LoadedRunPackage.load(outcome.run_dir)

        self.assertIn("SUITE_PLAN_UNSUPPORTED", {f.code for f in loaded.load_findings()})
        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertEqual(read_json(outcome.verdict_path)["status"], RunStatus.FAILED)

    def test_a_package_missing_its_build_manifest_is_incomplete_and_not_half_loaded(self):
        outcome = self.passing_run("e2e-r11-reader-no-build")
        (outcome.run_dir / BUILD_MANIFEST_FILENAME).unlink()

        package = LoadedRunPackage.load(outcome.run_dir)
        self.assertIsNone(
            package.build_manifest,
            msg="the reader populated a build manifest it never read",
        )
        self.assertIsNone(package.build_manifest_sha256)
        self.assertFalse(package.document_report()[BUILD_MANIFEST_FILENAME]["present"])

        graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.INCOMPLETE)
        self.assertIn("INCOMPLETE_BUILD_MANIFEST_MISSING", finding_codes(graded))

    def test_a_package_missing_its_lock_is_not_downgraded_to_a_suite_only_pass(self):
        outcome = self.passing_run("e2e-r11-reader-no-lock")
        (outcome.run_dir / RUN_LOCK_FILENAME).unlink()

        graded = grade_run_package(outcome.run_dir)

        self.assertEqual(graded.suite_status, ExecutionStatus.PASSED.value)
        self.assertEqual(
            graded.status,
            RunStatus.INCOMPLETE,
            msg="a passing suite was allowed to stand in for a whole run",
        )
        self.assertEqual(graded.exit_code, 1)
        self.assertIn("INCOMPLETE_LOCK_MISSING", finding_codes(graded))

    def test_a_package_whose_build_manifest_is_unreadable_is_rejected_not_guessed(self):
        outcome = self.passing_run("e2e-r11-reader-corrupt")
        (outcome.run_dir / BUILD_MANIFEST_FILENAME).write_text(
            "{ this is not json", encoding="utf-8"
        )

        package = LoadedRunPackage.load(outcome.run_dir)
        self.assertIsNone(package.build_manifest)
        self.assertTrue(package.document_report()[BUILD_MANIFEST_FILENAME]["present"])
        codes = {finding.code for finding in package.load_findings()}
        self.assertIn("DOCUMENT_UNREADABLE", codes)

        graded = evaluate_run(package)
        # A present but corrupt document is a failure, not an unfinished run.
        self.assertIn("INCOMPLETE_BUILD_MANIFEST_MISSING", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertEqual(graded.exit_code, 1)
        self.assertTrue(any(f.code == "DOCUMENT_UNREADABLE" and f.severity == BLOCKING
                            for f in graded.findings))

    def test_a_package_whose_execution_manifest_is_missing_keeps_its_suite_visible(self):
        """Incomplete, but still readable: recovery has to stay possible."""
        outcome = self.passing_run("e2e-r11-reader-no-execution")
        (outcome.run_dir / EXECUTION_MANIFEST_FILENAME).unlink()

        package = LoadedRunPackage.load(outcome.run_dir)
        self.assertIsNone(package.execution_manifest)
        # The suite is still found by the documented layout, so the reader can
        # say what was run even though the run never sealed itself.
        self.assertEqual(
            package.suite_dir, suite_export_dir(outcome.run_dir, outcome.run_id)
        )
        graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.INCOMPLETE)
        self.assertIn("INCOMPLETE_EXECUTION_MANIFEST_MISSING", finding_codes(graded))


# ---------------------------------------------------------------------------
# 5. offline re-derivation of the recorded provenance
# ---------------------------------------------------------------------------
class PackageDocumentAndOwnershipSafetyTests(RunPackageCase):
    def test_artifact_index_substituted_after_package_load_is_rejected(self):
        """Offline verification must not follow an index link added after loading.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("late-artifact-index-link")
        package = LoadedRunPackage.load(outcome.run_dir)
        index_path = package.suite_dir / "artifact-index.json"
        external_index = self.root / "external-artifact-index.json"
        index_path.rename(external_index)
        index_path.symlink_to(external_index)

        graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_VALIDATION_FAILED", finding_codes(graded))

    def test_artifact_index_substituted_during_suite_verification_is_rejected(self):
        """A link introduced after the checked index read cannot leave PASS.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        from forward_e2e.suite.verifier import verify_and_recalculate_suite

        outcome = self.passing_run("mid-verify-artifact-index-link")
        package = LoadedRunPackage.load(outcome.run_dir)
        index_path = package.suite_dir / "artifact-index.json"
        external_index = self.root / "external-mid-verify-index.json"

        def replace_then_verify(*args, **kwargs):
            index_path.rename(external_index)
            index_path.symlink_to(external_index)
            return verify_and_recalculate_suite(*args, **kwargs)

        with patch("forward_e2e.suite.verifier.verify_and_recalculate_suite", side_effect=replace_then_verify):
            graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_VALIDATION_FAILED", finding_codes(graded))

    def test_indexed_identity_swapped_only_during_hash_cannot_pass(self):
        """An indexed file must be hashed from the same package inode it names.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("transient-identity-link")
        identity_path = next(outcome.run_dir.glob("suite/*/runs/*/identity.json"))
        relative = identity_path.relative_to(identity_path.parents[2]).as_posix()
        external = self.root / "external-identity.json"
        external.write_bytes(identity_path.read_bytes() + b" \n")
        index_path = identity_path.parents[2] / "artifact-index.json"
        index = read_json(index_path)
        entry = next(item for item in index["artifacts"] if item["relative_path"] == relative)
        entry["sha256"] = hashlib.sha256(external.read_bytes()).hexdigest()
        entry["size_bytes"] = external.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")
        package = LoadedRunPackage.load(outcome.run_dir)

        original_digest = runpackage.sha256_size_checked_file
        substituted = False

        def digest_with_transient_link(path):
            nonlocal substituted
            if Path(path) != identity_path:
                return original_digest(path)
            substituted = True
            held = self.root / "held-identity.json"
            identity_path.rename(held)
            identity_path.symlink_to(external)
            try:
                return original_digest(path)
            finally:
                identity_path.unlink()
                held.rename(identity_path)

        with patch("forward_e2e.execution.runpackage.sha256_size_checked_file", side_effect=digest_with_transient_link):
            graded = evaluate_run(package)
        self.assertTrue(substituted)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))

    def test_identity_parse_uses_the_bytes_that_were_hashed(self):
        """A transient external identity cannot override the indexed package file.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("transient-identity-parse")
        identity_path = next(outcome.run_dir.glob("suite/*/runs/*/identity.json"))
        external = self.root / "correct-external-identity.json"
        external.write_bytes(identity_path.read_bytes())
        identity = read_json(identity_path)
        identity["gonka_source_sha"] = "d" * 40
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        index_path = identity_path.parents[2] / "artifact-index.json"
        index = read_json(index_path)
        relative = identity_path.relative_to(identity_path.parents[2]).as_posix()
        entry = next(item for item in index["artifacts"] if item["relative_path"] == relative)
        entry["sha256"] = hashlib.sha256(identity_path.read_bytes()).hexdigest()
        entry["size_bytes"] = identity_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")
        package = LoadedRunPackage.load(outcome.run_dir)

        original_reader = runpackage.read_checked_file_bytes
        substituted = False

        def read_with_transient_file(path):
            nonlocal substituted
            if Path(path) != identity_path or substituted:
                return original_reader(path)
            substituted = True
            held = self.root / "held-corrupt-identity.json"
            identity_path.rename(held)
            external.rename(identity_path)
            try:
                return original_reader(path)
            finally:
                identity_path.rename(external)
                held.rename(identity_path)

        with patch("forward_e2e.execution.runpackage.read_checked_file_bytes", side_effect=read_with_transient_file):
            graded = evaluate_run(package)
        self.assertTrue(substituted)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))

    def test_suite_plan_parse_uses_the_loaded_package_bytes(self):
        """A changed suite plan cannot replace the loaded suite selection.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("transient-suite-plan-parse")
        package = LoadedRunPackage.load(outcome.run_dir)
        plan_path = package.suite_dir / "suite-plan.json"
        plan = read_json(plan_path)
        plan["suite_id"] = "different-suite"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        original_read_text = Path.read_text

        def reject_plan_reopen(path, *args, **kwargs):
            if path == plan_path:
                self.fail("The verifier reopened suite-plan.json instead of using loaded bytes")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", new=reject_plan_reopen):
            graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_PLAN_CHANGED", finding_codes(graded))

    def test_missing_recorded_suite_result_cannot_be_reconstructed_to_pass(self):
        """Task files alone cannot replace the final recorded suite result.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("missing-recorded-suite-result")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        (suite_dir / "suite-result.json").unlink()
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        index["artifacts"] = [
            item for item in index["artifacts"]
            if item["relative_path"] != "suite-result.json"
        ]
        index["total_artifacts"] = len(index["artifacts"])
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertNotEqual(graded.exit_code, 0)
        self.assertIn("INCOMPLETE_SUITE_RESULT_MISSING", finding_codes(graded))

    def test_invalid_recorded_suite_result_cannot_be_reconstructed_to_pass(self):
        """An indexed JSON object without a valid SuiteResult is not final proof.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("invalid-recorded-suite-result")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        result_path = suite_dir / "suite-result.json"
        result_path.write_text("{}", encoding="utf-8")
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(
            item for item in index["artifacts"]
            if item["relative_path"] == "suite-result.json"
        )
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertNotEqual(graded.exit_code, 0)
        self.assertIn("SUITE_RESULT_INVALID", finding_codes(graded))

    def test_recorded_suite_result_source_identity_must_match_plan(self):
        """A final result for another source cannot attest this plan.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-suite-result-source")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        result_path = suite_dir / "suite-result.json"
        recorded = read_json(result_path)
        recorded["source_identity"]["gonka_commit_sha"] = "0" * 40
        result_path.write_text(json.dumps(recorded), encoding="utf-8")
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(
            item for item in index["artifacts"]
            if item["relative_path"] == "suite-result.json"
        )
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))
        self.assertIn(
            "source identity differs",
            " ".join(
                finding.details.get("error", "")
                for finding in graded.findings
                if finding.code == "SUITE_INTEGRITY_VIOLATION"
            ),
        )

    def test_recorded_task_metadata_must_match_suite_plan(self):
        """The final suite record cannot quietly rename a planned task's role.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        changes = (
            ("ordinal", 99, "ordinal differs"),
            ("proof_level", "GO_BOUNDARY", "proof level differs"),
            ("run_id", "foreign-run", "run ID differs"),
        )
        for field, value, expected_error in changes:
            with self.subTest(field=field):
                outcome = self.passing_run(f"foreign-suite-task-{field}")
                suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
                result_path = suite_dir / "suite-result.json"
                recorded = read_json(result_path)
                recorded["tasks"][0][field] = value
                result_path.write_text(json.dumps(recorded), encoding="utf-8")
                index_path = suite_dir / "artifact-index.json"
                index = read_json(index_path)
                entry = next(
                    item for item in index["artifacts"]
                    if item["relative_path"] == "suite-result.json"
                )
                entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
                entry["size_bytes"] = result_path.stat().st_size
                index_path.write_text(json.dumps(index), encoding="utf-8")

                graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertIn(
                    expected_error,
                    " ".join(
                        finding.details.get("error", "")
                        for finding in graded.findings
                        if finding.code == "SUITE_INTEGRITY_VIOLATION"
                    ),
                )

    def test_recorded_passing_suite_cannot_hide_failed_task(self):
        """A final PASS must agree with the task statuses in that same record.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("contradictory-recorded-task")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        result_path = suite_dir / "suite-result.json"
        recorded = read_json(result_path)
        recorded["tasks"][0]["execution_status"] = "FAILED"
        result_path.write_text(json.dumps(recorded), encoding="utf-8")
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(
            item for item in index["artifacts"]
            if item["relative_path"] == "suite-result.json"
        )
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn(
            "Recorded suite status differs from its task outcomes",
            " ".join(
                finding.details.get("error", "")
                for finding in graded.findings
                if finding.code == "SUITE_INTEGRITY_VIOLATION"
            ),
        )

    def test_recorded_suite_creation_time_must_match_plan(self):
        """The final record must belong to the selected plan's launch.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-suite-creation-time")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        result_path = suite_dir / "suite-result.json"
        recorded = read_json(result_path)
        recorded["created_at_utc"] = "2000-01-01T00:00:00+00:00"
        result_path.write_text(json.dumps(recorded), encoding="utf-8")
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(
            item for item in index["artifacts"]
            if item["relative_path"] == "suite-result.json"
        )
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn(
            "Recorded suite creation time differs",
            " ".join(
                finding.details.get("error", "")
                for finding in graded.findings
                if finding.code == "SUITE_INTEGRITY_VIOLATION"
            ),
        )

    def test_indexed_task_result_metadata_must_match_suite_plan(self):
        """An indexed per-task result cannot override correct suite metadata.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-indexed-task-run-id")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        task_path = next(suite_dir.glob("runs/*/result.json"))
        task = read_json(task_path)
        task["run_id"] = "foreign-run"
        task_path.write_text(json.dumps(task), encoding="utf-8")
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        rel = task_path.relative_to(suite_dir).as_posix()
        entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
        entry["sha256"] = hashlib.sha256(task_path.read_bytes()).hexdigest()
        entry["size_bytes"] = task_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn(
            "Task result lock-exact-e run ID differs",
            " ".join(
                finding.details.get("error", "")
                for finding in graded.findings
                if finding.code == "SUITE_INTEGRITY_VIOLATION"
            ),
        )

    def test_indexed_run_artifact_cannot_name_foreign_task(self):
        """An indexed run artifact must name the task owning its run directory.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-indexed-artifact-task")
        suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(
            item for item in index["artifacts"]
            if item["relative_path"].startswith("runs/")
        )
        entry["task_id"] = "foreign-task"
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn(
            "links task 'foreign-task'",
            " ".join(
                finding.details.get("error", "")
                for finding in graded.findings
                if finding.code == "SUITE_INTEGRITY_VIOLATION"
            ),
        )

    def test_indexed_run_artifact_requires_run_metadata(self):
        """Missing ownership fields cannot attest a planned run artifact.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        for field in ("run_id", "task_id"):
            with self.subTest(field=field):
                outcome = self.passing_run(f"missing-index-{field}")
                suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
                index_path = suite_dir / "artifact-index.json"
                index = read_json(index_path)
                entry = next(
                    item for item in index["artifacts"]
                    if item["relative_path"].startswith("runs/")
                )
                entry.pop(field)
                index_path.write_text(json.dumps(index), encoding="utf-8")

                graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))

    def test_invalid_artifact_index_metadata_cannot_pass(self):
        """Replay must reject malformed index records even when hashes match.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        cases = ("schema", "count", "duplicate", "size")
        for case in cases:
            with self.subTest(case=case):
                outcome = self.passing_run(f"invalid-artifact-index-{case}")
                suite_dir = suite_export_dir(outcome.run_dir, outcome.run_id)
                index_path = suite_dir / "artifact-index.json"
                index = read_json(index_path)
                if case == "schema":
                    index["schema_version"] = "future-version"
                elif case == "count":
                    index["total_artifacts"] += 1
                elif case == "duplicate":
                    index["artifacts"].append(dict(index["artifacts"][0]))
                    index["total_artifacts"] += 1
                else:
                    index["artifacts"][0]["size_bytes"] += 1
                index_path.write_text(json.dumps(index), encoding="utf-8")

                graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))

    def test_loaded_failed_suite_result_cannot_be_replaced_before_grading(self):
        """A failed recorded suite cannot be swapped for a passing copy.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("swapped-suite-result")
        result_path = suite_export_dir(outcome.run_dir, outcome.run_id) / "suite-result.json"
        passing_result = result_path.read_bytes()
        failed_result = json.loads(passing_result)
        failed_result["overall_status"] = "FAILED"
        result_path.write_text(json.dumps(failed_result), encoding="utf-8")
        package = LoadedRunPackage.load(outcome.run_dir)
        result_path.write_bytes(passing_result)

        graded = evaluate_run(package)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_RESULT_CHANGED", finding_codes(graded))

    def test_recorded_failed_suite_cannot_be_promoted_by_task_recalculation(self):
        """Recorded failure and passing task files disagree, so no PASS is safe.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("contradictory-suite-result")
        result_path = suite_export_dir(outcome.run_dir, outcome.run_id) / "suite-result.json"
        result = read_json(result_path)
        result["overall_status"] = "FAILED"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        index_path = result_path.parent / "artifact-index.json"
        index = read_json(index_path)
        entry = next(item for item in index["artifacts"] if item["relative_path"] == "suite-result.json")
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        graded = evaluate_run(LoadedRunPackage.load(outcome.run_dir))
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_RESULT_DISAGREEMENT", finding_codes(graded))

    def test_parsed_task_result_must_match_its_indexed_bytes(self):
        """A transient passing task file cannot conceal an indexed failure.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("transient-task-result")
        result_path = next(outcome.run_dir.glob("suite/*/runs/*/result.json"))
        passing_bytes = result_path.read_bytes()
        failed_result = json.loads(passing_bytes)
        failed_result["execution_status"] = "FAILED"
        result_path.write_text(json.dumps(failed_result), encoding="utf-8")
        suite_dir = result_path.parents[2]
        rel = result_path.relative_to(suite_dir).as_posix()
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
        entry["sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        entry["size_bytes"] = result_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")
        package = LoadedRunPackage.load(outcome.run_dir)

        original_read_text = Path.read_text

        def substitute_task_result(path, *args, **kwargs):
            if path == result_path:
                return passing_bytes.decode("utf-8")
            return original_read_text(path, *args, **kwargs)

        original_checked_read = runpackage.read_checked_file_bytes
        substituted = False

        def checked_read_with_transient_result(path):
            nonlocal substituted
            if Path(path) != result_path:
                return original_checked_read(path)
            substituted = True
            held = self.root / "held-failed-task-result.json"
            result_path.rename(held)
            result_path.write_bytes(passing_bytes)
            try:
                return original_checked_read(path)
            finally:
                result_path.unlink()
                held.rename(result_path)

        with patch.object(Path, "read_text", new=substitute_task_result), patch.object(
            runpackage, "read_checked_file_bytes", side_effect=checked_read_with_transient_result,
        ):
            graded = evaluate_run(package)
        self.assertTrue(substituted)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))

    def test_structured_evidence_parse_must_match_its_indexed_bytes(self):
        """A transient valid live context cannot conceal indexed invalid evidence.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        from forward_e2e.suite.verifier import verify_and_recalculate_suite

        outcome = self.passing_run("transient-live-context")
        context_path = next((outcome.run_dir / "suite").rglob("live-context.json"))
        passing_bytes = context_path.read_bytes()
        invalid = json.loads(passing_bytes)
        invalid["run_id"] = "wrong-run"
        context_path.write_text(json.dumps(invalid), encoding="utf-8")
        suite_dir = next((outcome.run_dir / "suite").iterdir())
        rel = context_path.relative_to(suite_dir).as_posix()
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
        entry["sha256"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
        entry["size_bytes"] = context_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        original_read_text = Path.read_text

        def substitute_context(path, *args, **kwargs):
            if path == context_path:
                return passing_bytes.decode("utf-8")
            return original_read_text(path, *args, **kwargs)

        original_checked_read = runpackage.read_checked_file_bytes
        substituted = False

        def checked_read_with_transient_context(path):
            nonlocal substituted
            if Path(path) != context_path:
                return original_checked_read(path)
            substituted = True
            held = self.root / "held-invalid-live-context.json"
            context_path.rename(held)
            context_path.write_bytes(passing_bytes)
            try:
                return original_checked_read(path)
            finally:
                context_path.unlink()
                held.rename(context_path)

        with patch.object(Path, "read_text", new=substitute_context), patch.object(
            runpackage, "read_checked_file_bytes", side_effect=checked_read_with_transient_context,
        ):
            verification = verify_and_recalculate_suite(
                suite_dir,
                artifact_index_bytes=original_checked_read(index_path),
                artifact_index_checked=True,
                artifact_hasher=runpackage.sha256_checked_file,
                artifact_reader=runpackage.read_checked_file_bytes,
            )
        self.assertTrue(verification.integrity_errors)
        self.assertIn("live-context.json", " ".join(verification.integrity_errors))
        self.assertTrue(substituted)

    def test_execution_plan_id_must_match_the_preserved_lock(self):
        """An execution manifest cannot claim a different plan than its lock.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("execution-foreign-plan")
        path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
        manifest = read_json(path)
        manifest["plan_id"] = "another-plan"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("EXECUTION_PLAN_FOREIGN", finding_codes(graded))

    def test_resealed_build_plan_id_must_match_the_preserved_lock(self):
        """A sealed build cannot claim a different plan than its lock.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("build-foreign-plan")
        reseal_build_manifest(
            outcome.run_dir,
            lambda manifest: setattr(manifest, "plan_id", "another-plan"),
        )

        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("BUILD_PLAN_FOREIGN", finding_codes(graded))

    def test_execution_artifact_index_pointer_must_name_the_verified_index(self):
        """The execution manifest must identify the index being verified.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-index-pointer")
        path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
        manifest = read_json(path)
        self.assertTrue(manifest["artifact_index_relpath"])
        manifest["artifact_index_relpath"] = "other/artifact-index.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("ARTIFACT_INDEX_POINTER_MISMATCH", finding_codes(graded))

    def test_execution_delivery_pointer_must_name_the_loaded_ledger(self):
        """The execution manifest must identify the delivery ledger being graded.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        outcome = self.passing_run("foreign-delivery-pointer")
        path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
        manifest = read_json(path)
        self.assertEqual(manifest["delivery_manifest_relpath"], DELIVERY_MANIFEST_FILENAME)
        manifest["delivery_manifest_relpath"] = "other-delivery.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("DELIVERY_POINTER_MISMATCH", finding_codes(graded))

    def test_build_manifest_replaced_by_symlink_after_check_is_not_loaded(self):
        outcome = self.passing_run("document-source-race")
        document = outcome.run_dir / BUILD_MANIFEST_FILENAME
        outside = self.root / "outside-build-manifest.json"
        real_assert = runpackage.assert_no_symlinks_in_path
        substituted = False

        def swap_after_check(path, *, what):
            nonlocal substituted
            result = real_assert(path, what=what)
            if Path(path) == document and not substituted:
                substituted = True
                document.rename(outside)
                document.symlink_to(outside)
            return result

        with patch("forward_e2e.execution.runpackage.assert_no_symlinks_in_path", side_effect=swap_after_check):
            package = LoadedRunPackage.load(outcome.run_dir)
        self.assertTrue(substituted)
        self.assertIsNone(package.build_manifest)
        self.assertTrue(any(f.code == "DOCUMENT_PATH_UNSAFE" for f in package.load_findings()))

    def test_invalid_runtime_summary_shapes_replace_a_saved_pass_with_a_document_failure(self):
        cases = (
            ("contexts-number", ("live_contexts",), 1),
            ("contexts-null", ("live_contexts",), None),
            ("contexts-object", ("live_contexts",), {}),
            ("context-null", ("live_contexts", 0), None),
            ("comparisons-number", ("live_contexts", 0, "comparisons"), 1),
            ("comparison-null", ("live_contexts", 0, "comparisons", 0), None),
            ("missing-number", ("missing_evidence",), 1),
            ("missing-entry", ("missing_evidence",), [None]),
        )
        for name, field_path, value in cases:
            with self.subTest(corruption=name):
                outcome = self.passing_run(name)
                self.assertEqual(read_json(outcome.verdict_path)["status"], "PASSED")

                def corrupt(manifest):
                    target = manifest.observed_runtime
                    for key in field_path[:-1]:
                        target = target[key]
                    target[field_path[-1]] = value

                reseal_build_manifest(outcome.run_dir, corrupt)
                graded = grade_run_package(outcome.run_dir)
                self.assertEqual(graded.status, RunStatus.FAILED)
                self.assertIn("DOCUMENT_UNREADABLE", finding_codes(graded))
                self.assertEqual(read_json(outcome.verdict_path)["status"], "FAILED")

    def test_a_directory_at_each_document_path_is_present_but_invalid_instead_of_missing(self):
        for name in (*REQUIRED_DOCUMENTS, DELIVERY_MANIFEST_FILENAME):
            with self.subTest(document=name):
                outcome = self.passing_run("directory-" + name.replace(".", "-"))
                path = outcome.run_dir / name
                path.unlink()
                path.mkdir()
                graded = grade_run_package(outcome.run_dir)
                self.assertEqual(graded.status, RunStatus.FAILED)
                self.assertTrue(graded.documents[name]["present"])
                self.assertTrue(any(f.code == "DOCUMENT_UNREADABLE" and
                                    f.details.get("document") == name for f in graded.findings))
                self.assertEqual(read_json(outcome.verdict_path)["status"], "FAILED")

    def test_a_destination_created_at_publication_is_preserved_and_reported_as_an_export_conflict(self):
        source = self.root / "export-source"
        source.mkdir()
        (source / "evidence").write_bytes(b"SOURCE")
        destination = self.root / "export-destination"
        real_link = os.link

        def competing_writer(src, dst, **kwargs):
            (destination / "evidence").write_bytes(b"OTHER WRITER")
            return real_link(src, dst, **kwargs)

        with patch("forward_e2e.execution.file_safety.os.link", side_effect=competing_writer):
            with self.assertRaises(ExportConflict) as failure:
                export_run_package(source, destination)
        self.assertEqual(failure.exception.details["conflicts"][0]["path"], "evidence")
        self.assertEqual((destination / "evidence").read_bytes(), b"OTHER WRITER")
        self.assertEqual(list(destination.iterdir()), [destination / "evidence"])
        self.assertEqual((source / "evidence").read_bytes(), b"SOURCE")

    def test_each_core_document_symlink_is_rejected_before_reading_its_external_target(self):
        for name in ("run.lock.json", "build-manifest.json", "execution-manifest.json", "delivery.json"):
            with self.subTest(document=name):
                outcome = self.passing_run("symlink-" + name.replace(".", "-"))
                path = outcome.run_dir / name
                target = self.root / ("external-" + name)
                path.rename(target)
                path.symlink_to(target)
                original_open = Path.open
                def guarded_open(candidate, *args, **kwargs):
                    if candidate == path or candidate == target:
                        self.fail("The loader read a document symlink before rejecting it")
                    return original_open(candidate, *args, **kwargs)
                with patch.object(Path, "open", guarded_open):
                    package = LoadedRunPackage.load(outcome.run_dir)
                self.assertTrue(any(f.code in {"DOCUMENT_PATH_UNSAFE", "DELIVERY_MANIFEST_SYMLINK"}
                                    for f in package.load_findings()))
                self.assertNotEqual(grade_run_package(outcome.run_dir).status, RunStatus.PASSED)

    def test_missing_manifest_fields_and_invalid_utf8_produce_a_new_nonpassing_verdict(self):
        for name in ("build-manifest.json", "execution-manifest.json"):
            for corruption in ("missing-key", "utf8"):
                with self.subTest(document=name, corruption=corruption):
                    outcome = self.passing_run(corruption + "-" + name.replace(".", "-"))
                    path = outcome.run_dir / name
                    if corruption == "utf8":
                        path.write_bytes(b"\xff")
                    else:
                        data = read_json(path)
                        del data["run_id"]
                        path.write_text(json.dumps(data))
                    graded = grade_run_package(outcome.run_dir)
                    self.assertNotEqual(graded.status, RunStatus.PASSED)
                    self.assertIn("DOCUMENT_UNREADABLE", finding_codes(graded))
                    self.assertNotEqual(read_json(outcome.run_dir / RUN_RESULT_FILENAME)["status"], "PASSED")

    def test_malformed_container_summaries_replace_a_saved_pass_with_a_document_failure(self):
        cases = (
            ("null-list", ("containers",), None),
            ("object-list", ("containers",), {}),
            ("scalar-list", ("containers",), 1),
            ("null-entry", ("containers", 0), None),
            ("invalid-path", ("containers", 0, "path"), []),
            ("null-findings", ("containers", 0, "findings"), None),
            ("invalid-finding", ("containers", 0, "findings", 0), None),
        )
        for name, field_path, value in cases:
            with self.subTest(corruption=name):
                outcome = self.passing_run("container-summary-" + name)
                self.assertEqual(read_json(outcome.verdict_path)["status"], "PASSED")

                def corrupt(manifest):
                    target = manifest.observed_runtime
                    for key in field_path[:-1]:
                        target = target[key]
                    target[field_path[-1]] = value

                reseal_build_manifest(outcome.run_dir, corrupt)
                graded = grade_run_package(outcome.run_dir)
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                errors = [f for f in graded.findings if f.code == "DOCUMENT_UNREADABLE"]
                self.assertTrue(errors, graded.findings)
                self.assertEqual(errors[0].details["document"], BUILD_MANIFEST_FILENAME)
                self.assertIn("observed_runtime.containers", errors[0].message)
                self.assertNotEqual(read_json(outcome.verdict_path)["status"], "PASSED")

    def test_raw_ownership_is_required_even_when_the_recorded_summary_still_says_it_matched(self):
        for corruption in ("missing", "foreign-image", "malformed", "missing-image"):
            with self.subTest(corruption=corruption):
                outcome = self.passing_run("raw-ownership-" + corruption)
                path = next((outcome.run_dir / "suite").rglob("cleanup-evidence/ownership.json"))
                if corruption == "missing":
                    path.unlink()
                elif corruption == "malformed":
                    path.write_text("{")
                else:
                    payload = read_json(path)
                    if corruption == "missing-image":
                        del payload["owned_containers"][0]["image"]
                    else:
                        payload["owned_containers"][0]["image"] = FOREIGN_IMAGE_ID
                    path.write_text(json.dumps(payload))
                graded = grade_run_package(outcome.run_dir)
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertTrue(any(code.startswith("OWNERSHIP_EVIDENCE_") for code in finding_codes(graded)))

    def test_offline_report_rejects_foreign_run_ids_and_unconfirmed_ownership(self):
        for field, value in (("run_id", "other-task"), ("run_id", None),
                             ("ownership_verified", False), ("ownership_verified", None),
                             ("ownership_verified", 1), ("ownership_verified", "true")):
            with self.subTest(field=field, value=value):
                outcome = self.passing_run(f"metadata-{field}-{value}")
                path = next((outcome.run_dir / "suite").rglob("cleanup-evidence/ownership.json"))
                payload = read_json(path)
                if value is None:
                    del payload[field]
                else:
                    payload[field] = value
                path.write_text(json.dumps(payload))
                graded = grade_run_package(outcome.run_dir)
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertIn("OWNERSHIP_EVIDENCE_INVALID", finding_codes(graded))

    def test_a_stale_summary_is_rejected_even_when_both_image_ids_belong_to_the_build(self):
        outcome = self.passing_run("ownership-stale-summary")
        def add_second_image(manifest):
            image = dict(manifest.images[0])
            image["image_id"] = FOREIGN_IMAGE_ID
            manifest.images.append(image)
        reseal_build_manifest(outcome.run_dir, add_second_image)
        self.assertEqual(grade_run_package(outcome.run_dir).status, RunStatus.PASSED)
        path = next((outcome.run_dir / "suite").rglob("cleanup-evidence/ownership.json"))
        payload = read_json(path)
        payload["owned_containers"][0]["image"] = FOREIGN_IMAGE_ID
        path.write_text(json.dumps(payload))
        graded = grade_run_package(outcome.run_dir)
        self.assertIn("OWNERSHIP_SUMMARY_MISMATCH", finding_codes(graded))

    def test_pattern_mismatches_and_invalid_patterns_are_not_skipped_for_null_expected_values(self):
        for corruption in ("actual", "pattern"):
            with self.subTest(corruption=corruption):
                outcome = self.passing_run("regex-" + corruption)
                def change_comparison(manifest):
                    for comparison in manifest.observed_runtime["live_contexts"][0]["comparisons"]:
                        if comparison["kind"] == "literal_pattern":
                            if corruption == "actual":
                                comparison["actual"] = "v99.0.0"
                            else:
                                comparison["source_of_truth"] = "["
                reseal_build_manifest(outcome.run_dir, change_comparison)
                graded = grade_run_package(outcome.run_dir)
                self.assertIn("RUNTIME_MISMATCH", finding_codes(graded))
                self.assertNotEqual(graded.status, RunStatus.PASSED)


class OfflineProvenanceRederivationTests(RunPackageCase):
    """F3: the reader replays the stored comparisons instead of trusting them."""

    def recorded_comparisons(self, package_root: Path) -> list:
        manifest = BuildManifest.from_dict(read_json(package_root / BUILD_MANIFEST_FILENAME))
        contexts = manifest.observed_runtime.get("live_contexts") or []
        self.assertTrue(contexts, msg="the run recorded no live-context comparisons")
        return contexts[0]["comparisons"]

    def test_the_run_records_the_two_comparison_shapes_the_reader_has_to_tell_apart(self):
        outcome = self.passing_run("e2e-r11-comparison-shapes")
        comparisons = self.recorded_comparisons(outcome.run_dir)
        by_kind = {entry["kind"]: entry for entry in comparisons}
        self.assertEqual(
            set(by_kind),
            {ExpectationKind.SELECTED_COMMIT.value, ExpectationKind.LITERAL_PATTERN.value},
        )
        # This is the producer's shape: no "status" key at all, and a literal
        # pattern legitimately has no expected value to compare against.
        self.assertIsNone(by_kind[ExpectationKind.LITERAL_PATTERN.value]["expected"])
        self.assertNotIn("status", by_kind[ExpectationKind.SELECTED_COMMIT.value])
        self.assertEqual(
            by_kind[ExpectationKind.SELECTED_COMMIT.value]["expected"], REQUESTED_SHA
        )

    def test_a_literal_pattern_comparison_without_an_expected_value_is_checked_against_its_pattern(self):
        outcome = self.passing_run("e2e-r11-literal-skipped")
        package = LoadedRunPackage.load(outcome.run_dir)
        findings = package.provenance_findings()
        self.assertEqual(
            [f.code for f in findings],
            [],
            msg="an unmeasurable expectation was counted as a runtime mismatch",
        )
        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.PASSED)

    def test_a_recorded_comparison_whose_values_disagree_is_re_derived_as_a_mismatch(self):
        """One fact changed: the observed value of the selected-commit comparison."""
        outcome = self.passing_run("e2e-r11-runtime-mismatch")

        def swap_observed(manifest: BuildManifest) -> None:
            entry = manifest.observed_runtime["live_contexts"][0]
            for comparison in entry["comparisons"]:
                if comparison["kind"] == ExpectationKind.SELECTED_COMMIT.value:
                    comparison["actual"] = "d" * 40

        reseal_build_manifest(outcome.run_dir, swap_observed)
        graded = grade_run_package(outcome.run_dir)

        self.assertIn("RUNTIME_MISMATCH", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertEqual(graded.suite_status, ExecutionStatus.PASSED.value)

    def test_a_comparison_marked_as_matching_whose_own_values_disagree_is_inconsistent(self):
        outcome = self.passing_run("e2e-r11-runtime-inconsistent")

        def claim_a_match(manifest: BuildManifest) -> None:
            entry = manifest.observed_runtime["live_contexts"][0]
            for comparison in entry["comparisons"]:
                if comparison["kind"] == ExpectationKind.SELECTED_COMMIT.value:
                    comparison["status"] = "MATCHED"

        # Establish a consistent positive example with an explicit match claim;
        # the negative then changes only the observed commit.
        reseal_build_manifest(outcome.run_dir, claim_a_match)
        self.assertEqual(grade_run_package(outcome.run_dir).status, RunStatus.PASSED)

        def change_actual(manifest: BuildManifest) -> None:
            comparisons = manifest.observed_runtime["live_contexts"][0]["comparisons"]
            comparison = next(item for item in comparisons
                              if item["kind"] == ExpectationKind.SELECTED_COMMIT.value)
            comparison["actual"] = "d" * 40

        reseal_build_manifest(outcome.run_dir, change_actual)
        graded = grade_run_package(outcome.run_dir)

        self.assertIn("RUNTIME_COMPARISON_INCONSISTENT", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)

    def test_container_findings_are_read_from_the_key_the_producer_actually_writes(self):
        outcome = self.passing_run("e2e-r11-container-key")
        produced = verify_running_images(
            expected_images=[{"role": "node", "image_id": RUNNER_IMAGE_ID}],
            running={"genesis-node": RUNNER_IMAGE_ID},
        )
        self.assertEqual(
            sorted(produced[0]),
            [
                "container",
                "from_this_build",
                "image_id",
                "image_reference",
                "pinned_runtime_dependency",
            ],
        )
        self.assertNotIn(
            "status",
            produced[0],
            msg="the producer never writes a status key, so the reader may not need one",
        )

        task_run_id = make_task_run_id(outcome.run_id, 1, SELECTED_TASK_ID)
        foreign = {
            "container": "genesis-node",
            "image_id": FOREIGN_IMAGE_ID,
            "image_reference": "",
            "from_this_build": False,
            "pinned_runtime_dependency": False,
        }
        self.assertEqual(sorted(foreign), sorted(produced[0]))

        def record_container(manifest: BuildManifest) -> None:
            manifest.observed_runtime["containers"] = [
                {
                    "path": f"runs/{task_run_id}/cleanup-evidence/ownership.json",
                    "findings": [foreign],
                }
            ]

        reseal_build_manifest(outcome.run_dir, record_container)
        graded = grade_run_package(outcome.run_dir)

        self.assertIn("RUNNING_IMAGE_MISMATCH", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)

    def test_a_container_finding_from_this_build_is_not_reported_as_a_mismatch(self):
        """The positive twin: the same key, the other value, no finding."""
        outcome = self.passing_run("e2e-r11-container-ok")
        task_run_id = make_task_run_id(outcome.run_id, 1, SELECTED_TASK_ID)

        def record_container(manifest: BuildManifest) -> None:
            # Compare against the images recorded by the fixture's real build.
            self.assertTrue(manifest.images)
            actual_produced = verify_running_images(
                expected_images=manifest.images,
                running={
                    "genesis-node": {
                        "image": RUNNER_IMAGE_ID,
                        "image_reference": "gonka/inference-chain:e2e",
                    }
                },
            )
            manifest.observed_runtime["containers"] = [
                {
                    "path": f"runs/{task_run_id}/cleanup-evidence/ownership.json",
                    "findings": actual_produced,
                }
            ]

        reseal_build_manifest(outcome.run_dir, record_container)
        graded = grade_run_package(outcome.run_dir)

        self.assertNotIn("RUNNING_IMAGE_MISMATCH", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.PASSED)

    def test_a_true_external_pin_flag_without_locked_proof_is_rejected(self):
        outcome = self.passing_run("e2e-r11-pinned-runtime")
        task_run_id = make_task_run_id(outcome.run_id, 1, SELECTED_TASK_ID)
        pinned = {
            "container": "genesis-postgres",
            "image_id": FOREIGN_IMAGE_ID,
            "image_reference": "postgres:18.1-bookworm",
            "from_this_build": False,
            "pinned_runtime_dependency": True,
        }

        def record_container(manifest: BuildManifest) -> None:
            manifest.observed_runtime["containers"] = [
                {
                    "path": f"runs/{task_run_id}/cleanup-evidence/ownership.json",
                    "findings": [pinned],
                }
            ]

        reseal_build_manifest(outcome.run_dir, record_container)
        graded = grade_run_package(outcome.run_dir)

        self.assertIn("RUNNING_IMAGE_MISMATCH", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)

    def test_evidence_the_run_itself_recorded_as_missing_is_carried_into_the_verdict(self):
        outcome = self.passing_run("e2e-r11-missing-at-runtime")

        def record_missing(manifest: BuildManifest) -> None:
            manifest.observed_runtime["missing_evidence"] = [
                {
                    "task_id": SELECTED_TASK_ID,
                    "run_id": make_task_run_id(outcome.run_id, 1, SELECTED_TASK_ID),
                    "reason": "the task recorded no live-context.json",
                }
            ]

        reseal_build_manifest(outcome.run_dir, record_missing)
        graded = grade_run_package(outcome.run_dir)

        self.assertIn("TASK_EVIDENCE_MISSING_AT_RUNTIME", finding_codes(graded))
        self.assertEqual(graded.status, RunStatus.FAILED)

    def test_malformed_source_immutability_summaries_replace_a_saved_pass_with_a_document_failure(self):
        cases = (
            ("null-list", ("source_immutability",), None),
            ("object-list", ("source_immutability",), {}),
            ("null-entry", ("source_immutability", 0), None),
            ("invalid-path", ("source_immutability", 0, "path"), []),
            ("invalid-verified", ("source_immutability", 0, "verified"), "true"),
        )
        for name, field_path, value in cases:
            with self.subTest(corruption=name):
                outcome = self.passing_run("source-immut-summary-" + name)
                self.assertEqual(read_json(outcome.verdict_path)["status"], "PASSED")

                def corrupt(manifest):
                    target = manifest.observed_runtime
                    for key in field_path[:-1]:
                        target = target[key]
                    target[field_path[-1]] = value

                reseal_build_manifest(outcome.run_dir, corrupt)
                graded = grade_run_package(outcome.run_dir)
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                errors = [f for f in graded.findings if f.code == "DOCUMENT_UNREADABLE"]
                self.assertTrue(errors, graded.findings)
                self.assertEqual(errors[0].details["document"], BUILD_MANIFEST_FILENAME)
                self.assertIn("observed_runtime.source_immutability", errors[0].message)

    def test_source_immutability_tampering_on_disk_is_rejected_offline(self):
        outcome = self.passing_run("e2e-r11-source-immut-tampered")
        immut_path = next((outcome.run_dir / "suite").rglob("source-immutability.json"))
        doc = read_json(immut_path)
        doc["verdict"] = "VIOLATED"
        immut_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        graded = grade_run_package(outcome.run_dir)
        self.assertEqual(graded.status, RunStatus.FAILED)
        self.assertIn("SUITE_INTEGRITY_VIOLATION", finding_codes(graded))
        self.assertIn("PROVENANCE_EVIDENCE_UNREADABLE", finding_codes(graded))

    def test_provenance_replay_must_use_indexed_live_context_bytes(self):
        """A transient valid context cannot hide an indexed deployment mismatch.

        The fixture is synthetic; no network, Docker, or live chain calls.
        """
        outcome = self.passing_run("e2e-r11-transient-provenance-context")
        context_path = next((outcome.run_dir / "suite").rglob("live-context.json"))
        passing_bytes = context_path.read_bytes()
        changed = json.loads(passing_bytes)
        original_source = changed["source"]

        def record_built_wasm(manifest: BuildManifest) -> None:
            manifest.deployment["a9_manifest_sha256"] = original_source["a9_manifest_sha256"]
            manifest.deployment["production_wasm"] = [
                {"role": "deal", "sha256": original_source["a9_contract_sha256"]["deal"]},
                {"role": "factory", "sha256": original_source["a9_contract_sha256"]["factory"]},
            ]

        reseal_build_manifest(outcome.run_dir, record_built_wasm)
        changed["source"]["a9_manifest_sha256"] = "d" * 64
        context_path.write_text(json.dumps(changed), encoding="utf-8")
        suite_dir = context_path.parents[2]
        index_path = suite_dir / "artifact-index.json"
        index = read_json(index_path)
        rel = context_path.relative_to(suite_dir).as_posix()
        entry = next(item for item in index["artifacts"] if item["relative_path"] == rel)
        entry["sha256"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
        entry["size_bytes"] = context_path.stat().st_size
        index_path.write_text(json.dumps(index), encoding="utf-8")

        self.assertIn("DEPLOYED_ARTEFACT_MISMATCH", finding_codes(grade_run_package(outcome.run_dir)))
        package = LoadedRunPackage.load(outcome.run_dir)
        original_verify = package._verify_suite_artifacts
        original_read = runpackage.read_checked_file_bytes
        suite_checked = False

        def verify_then_allow_swap():
            nonlocal suite_checked
            result = original_verify()
            suite_checked = True
            return result

        def transient_context(path):
            if suite_checked and Path(path) == context_path:
                return passing_bytes
            return original_read(path)

        with patch.object(package, "_verify_suite_artifacts", side_effect=verify_then_allow_swap), \
                patch.object(runpackage, "read_checked_file_bytes", side_effect=transient_context):
            graded = evaluate_run(package)
        self.assertNotEqual(graded.status, RunStatus.PASSED)
        self.assertIn("PROVENANCE_EVIDENCE_UNREADABLE", finding_codes(graded))

    def test_provenance_document_removed_after_task_evidence_cannot_pass(self):
        """A disappearing mandatory document cannot be treated as already checked.

        The fixture is synthetic; no network, Docker, or live chain calls.
        """
        for name in ("live-context.json", "source-immutability.json", "network-manifest.json"):
            with self.subTest(document=name):
                outcome = self.passing_run("e2e-r11-late-missing-" + name.removesuffix(".json"))
                package = LoadedRunPackage.load(outcome.run_dir)
                document_path = next((outcome.run_dir / "suite").rglob(name))
                original_compute = package._compute_task_evidence

                def locate_then_remove():
                    result = original_compute()
                    document_path.unlink()
                    return result

                with patch.object(package, "_compute_task_evidence", side_effect=locate_then_remove):
                    graded = evaluate_run(package)
                self.assertNotEqual(graded.status, RunStatus.PASSED)
                self.assertIn("PROVENANCE_EVIDENCE_UNREADABLE", finding_codes(graded))

    def test_task_evidence_rejects_a_link_inserted_after_package_loading(self):
        """A later link swap must not make external evidence satisfy the task. All fixtures are synthetic. No network, Docker, or live chain calls."""
        outcome = self.passing_run("e2e-r11-late-evidence-link")
        package = LoadedRunPackage.load(outcome.run_dir)
        live_path = next((outcome.run_dir / "suite").rglob("live-context.json"))
        external = self.root / "external-live-context.json"
        shutil.copyfile(live_path, external)
        live_path.unlink()
        live_path.symlink_to(external)

        _items, findings = package._compute_task_evidence()
        self.assertIn("DOCUMENT_UNREADABLE", {finding.code for finding in findings})
        self.assertIn("PROVENANCE_EVIDENCE_UNREADABLE",
                      {finding.code for finding in package.provenance_findings()})

    def test_provenance_rejects_a_source_record_link_inserted_after_loading(self):
        """A source proof link swap must block replay even when its target has valid JSON. All fixtures are synthetic. No network, Docker, or live chain calls."""
        outcome = self.passing_run("e2e-r11-late-source-link")
        package = LoadedRunPackage.load(outcome.run_dir)
        source_path = next((outcome.run_dir / "suite").rglob("source-immutability.json"))
        external = self.root / "external-source-immutability.json"
        shutil.copyfile(source_path, external)
        source_path.unlink()
        source_path.symlink_to(external)

        _items, findings = package._compute_task_evidence()
        self.assertIn("SOURCE_IMMUTABILITY_MISSING", {finding.code for finding in findings})
        self.assertIn("PROVENANCE_EVIDENCE_UNREADABLE",
                      {finding.code for finding in package.provenance_findings()})

    def test_provenance_report_exposes_source_immutability_build_inputs_and_staged_contexts(self):
        outcome = self.passing_run("e2e-r11-provenance-report-fields")
        package = LoadedRunPackage.load(outcome.run_dir)
        report = package.provenance_report()
        self.assertEqual(report["source_immutability"]["verdict"], "UNCHANGED")
        self.assertEqual(report["build_inputs"]["gonka_sha"], REQUESTED_SHA)
        self.assertEqual(report["build_inputs"]["contracts_sha"], CONTRACTS_SHA)
        self.assertIsInstance(report["staged_contexts"], list)
        self.assertEqual(report["observed_runtime_summary"]["source_immutability"], 1)


# ---------------------------------------------------------------------------
# 6. report and recover
# ---------------------------------------------------------------------------
class ReportAndRecoverResolutionTests(RunPackageCase):
    """F3/F4: pointing at a part is answered about the whole; recovery re-grades."""

    def report(self, target: Path, *, output: Path | None = None):
        messages: list = []
        _subcommand, args = parse_e2e_args(
            [
                "report",
                "--run",
                str(target),
                "--output",
                str(output or self.output_dir),
                "--workspace",
                str(self.workspace_dir),
            ]
        )
        code = cmd_report(args, emit=messages.append)
        return code, messages

    def recover(self, run_id: str, output: Path):
        messages: list = []
        _subcommand, args = parse_e2e_args(
            [
                "recover",
                "--run",
                run_id,
                "--output",
                str(output),
                "--workspace",
                str(self.workspace_dir),
            ]
        )
        code = cmd_recover(args, emit=messages.append)
        return code, messages

    def test_report_pointed_at_the_nested_suite_resolves_upward_to_the_run_package(self):
        outcome = self.provenance_failure_run("e2e-r11-report-nested")
        nested = suite_export_dir(outcome.run_dir, outcome.run_id)
        self.assertEqual(
            read_json(nested / "suite-result.json")["overall_status"],
            ExecutionStatus.PASSED.value,
        )

        code, messages = self.report(nested)

        self.assertEqual(
            code, 1, msg="a locally successful suite was reported as an E2E pass"
        )
        self.assertTrue(
            any("part of the run package at" in message for message in messages),
            msg="the upward resolution was never announced",
        )
        self.assertTrue(
            any(f"{RunStatus.FAILED} (exit 1)" in message for message in messages)
        )
        self.assertEqual(read_json(outcome.verdict_path)["status"], RunStatus.FAILED)

    def test_report_by_run_directory_and_by_nested_suite_give_the_same_verdict(self):
        outcome = self.provenance_failure_run("e2e-r11-report-same")
        by_directory, _ = self.report(outcome.run_dir)
        verdict_after_directory = read_json(outcome.verdict_path)["status"]
        by_suite, _ = self.report(suite_export_dir(outcome.run_dir, outcome.run_id))
        verdict_after_suite = read_json(outcome.verdict_path)["status"]

        self.assertEqual(by_directory, by_suite)
        self.assertEqual(verdict_after_directory, verdict_after_suite)
        self.assertEqual(verdict_after_suite, RunStatus.FAILED)

    def test_report_pointed_at_a_bare_a8_suite_says_it_is_not_an_e2e_verdict(self):
        legacy_output = self.root / "legacy-out"
        suite_id = "a8-legacy-suite-01"
        suite_dir = write_staged_suite(legacy_output / suite_id, suite_id)

        code, messages = self.report(suite_dir, output=legacy_output)

        joined = "\n".join(messages)
        self.assertIn("This directory is a bare suite directory, not an E2E run package", joined)
        self.assertIn("is not an E2E run verdict", joined)
        self.assertIn("Bare suite directories without an E2E run package envelope cannot produce a passing E2E verdict", joined)
        # A bare suite without an E2E run package envelope unconditionally fails
        # with exit code 1, and no E2E verdict document is invented for it.
        self.assertEqual(code, 1)
        self.assertFalse((suite_dir / RUN_RESULT_FILENAME).exists())
        self.assertEqual(list(suite_dir.glob(f"**/{RUN_RESULT_FILENAME}")), [])

    def test_recover_re_exports_the_durable_stage_and_re_grades_to_the_same_verdict(self):
        outcome = self.provenance_failure_run("e2e-r11-recover")
        original = read_json(outcome.verdict_path)
        # The host export is lost; only the durable workspace state remains.
        shutil.rmtree(outcome.run_dir)
        self.assertFalse(outcome.run_dir.exists())
        new_output = self.root / "recovered"

        code, messages = self.recover(outcome.run_id, new_output)

        recovered_dir = new_output / outcome.run_id
        for name in REQUIRED_DOCUMENTS:
            self.assertTrue(
                (recovered_dir / name).is_file(), msg=f"{name} was not recovered"
            )
            self.assertEqual(
                sha256_file(recovered_dir / name),
                sha256_file(outcome.stage_dir / name),
                msg=f"the recovered {name} is not the durable one",
            )
        recovered = read_json(recovered_dir / RUN_RESULT_FILENAME)
        self.assertEqual(recovered["status"], original["status"])
        self.assertEqual(recovered["status"], RunStatus.FAILED)
        self.assertEqual(recovered["exit_code"], original["exit_code"])
        self.assertEqual(code, original["exit_code"])
        self.assertEqual(
            {f["code"] for f in recovered["findings"]},
            {f["code"] for f in original["findings"]},
            msg="recovery changed why the run failed",
        )
        self.assertTrue(any("Verdict:" in message for message in messages))

    def test_recover_of_a_passing_run_keeps_it_passing_after_the_host_copy_is_lost(self):
        outcome = self.passing_run("e2e-r11-recover-passed")
        original = read_json(outcome.verdict_path)
        shutil.rmtree(outcome.run_dir)
        new_output = self.root / "recovered-passed"

        code, _messages = self.recover(outcome.run_id, new_output)

        recovered = read_json(new_output / outcome.run_id / RUN_RESULT_FILENAME)
        self.assertEqual(recovered["status"], RunStatus.PASSED)
        self.assertEqual(original["status"], RunStatus.PASSED)
        self.assertEqual(code, 0)

    def test_report_can_grade_the_durable_stage_itself_when_nothing_was_exported(self):
        run_id = "e2e-r11-report-stage"

        def occupy_destination(_kwargs):
            destination = self.run_dir(run_id)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / BUILD_MANIFEST_FILENAME).write_text(
                '{"schema_version": "not-this-run"}\n', encoding="utf-8"
            )

        outcome = self.execute(run_id, on_suite=occupy_destination)
        self.assertIsNone(outcome.error)

        # Before recover, report must preserve the export failure fact (exit 1)
        code, messages = self.report(outcome.stage_dir)

        self.assertEqual(code, 1)
        self.assertEqual(
            read_json(outcome.stage_dir / RUN_RESULT_FILENAME)["status"], RunStatus.FAILED
        )
        self.assertTrue(any("Verdict:" in message for message in messages))

        # Recover into a new output directory succeeds
        new_output = self.root / "recovered-report-stage"
        rec_code, _ = self.recover(run_id, new_output)
        self.assertEqual(rec_code, 0)
        rec_result = read_json(new_output / run_id / RUN_RESULT_FILENAME)
        self.assertEqual(rec_result["status"], RunStatus.PASSED)

    def test_report_rejects_suite_id_fallback_escaping_package_boundary_without_overwriting_external_summary(self):
        outcome = self.passing_run("e2e-r11-suite-id-escape")
        outside_suite = self.output_dir / "outside_suite"
        shutil.copytree(outcome.run_dir / "suite" / outcome.run_id, outside_suite)
        sentinel = "SENTINEL_EXTERNAL_SUMMARY_DO_NOT_OVERWRITE\n"
        (outside_suite / "summary.md").write_text(sentinel, encoding="utf-8")

        exec_manifest_path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
        exec_manifest = read_json(exec_manifest_path)
        exec_manifest["suite_id"] = f"../../{outside_suite.name}"
        self.assertEqual(
            (outcome.run_dir / "suite" / exec_manifest["suite_id"]).resolve(),
            outside_suite.resolve(),
        )
        exec_manifest.pop("suite_export_relpath", None)
        exec_manifest_path.write_text(
            json.dumps(exec_manifest, indent=2) + "\n", encoding="utf-8"
        )

        code, _messages = self.report(outcome.run_dir)

        self.assertEqual(code, 1)
        self.assertEqual(
            (outside_suite / "summary.md").read_text(encoding="utf-8"),
            sentinel,
        )
        verdict = read_json(outcome.verdict_path)
        self.assertEqual(verdict["status"], RunStatus.FAILED)
        self.assertIn(
            "SUITE_PATH_UNSAFE",
            {finding["code"] for finding in verdict["findings"]},
        )

    def test_report_rejects_individual_suite_file_symlinks_before_reading_or_modifying_external_targets(self):
        for fallback in (False, True):
            for filename in ("suite-plan.json", "suite-result.json", "live-context.json"):
                with self.subTest(fallback=fallback, file=filename):
                    outcome = self.passing_run(f"file-symlink-{fallback}-{filename.replace('.', '-')}")
                    if fallback:
                        execution_path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
                        execution = read_json(execution_path)
                        execution.pop("suite_export_relpath")
                        execution_path.write_text(json.dumps(execution), encoding="utf-8")
                        self.assertEqual(grade_run_package(outcome.run_dir).status, RunStatus.PASSED)
                    suite = suite_export_dir(outcome.run_dir, outcome.run_id)
                    candidates = list(suite.rglob(filename))
                    self.assertEqual(len(candidates), 1)
                    document = candidates[0]
                    external = self.root / f"external-{fallback}-{filename}"
                    document.rename(external)
                    original_bytes = external.read_bytes()
                    original_mtime = external.stat().st_mtime_ns
                    document.symlink_to(external)
                    original_open = Path.open
                    reads = []

                    def record_external_access(path, *args, **kwargs):
                        if path == document or path == external:
                            reads.append(path)
                        return original_open(path, *args, **kwargs)

                    with patch.object(Path, "open", record_external_access):
                        code, _ = self.report(outcome.run_dir)
                    self.assertEqual(reads, [], "The unsafe suite must be rejected before reading its files")
                    self.assertEqual(code, 1)
                    verdict = read_json(outcome.verdict_path)
                    self.assertEqual(verdict["status"], RunStatus.FAILED)
                    self.assertIn("SUITE_PATH_UNSAFE", {f["code"] for f in verdict["findings"]})
                    self.assertEqual(external.read_bytes(), original_bytes)
                    self.assertEqual(external.stat().st_mtime_ns, original_mtime)
                    self.assertTrue(document.is_symlink())

    def test_report_rejects_a_symlinked_intermediate_suite_directory_without_overwriting_target(self):
        outcome = self.passing_run("e2e-r11-suite-symlink")
        outside_target = self.root / "outside_symlink_suite"
        shutil.move(str(outcome.run_dir / "suite"), str(outside_target))
        sentinel = "SENTINEL_SYMLINK_SUMMARY_DO_NOT_OVERWRITE\n"
        (outside_target / outcome.run_id / "summary.md").write_text(sentinel, encoding="utf-8")
        (outcome.run_dir / "suite").symlink_to(outside_target, target_is_directory=True)

        exec_manifest_path = outcome.run_dir / EXECUTION_MANIFEST_FILENAME
        exec_manifest = read_json(exec_manifest_path)
        exec_manifest.pop("suite_export_relpath", None)
        exec_manifest_path.write_text(
            json.dumps(exec_manifest, indent=2) + "\n", encoding="utf-8"
        )

        code, _messages = self.report(outcome.run_dir)

        self.assertEqual(code, 1)
        self.assertEqual(
            (outside_target / outcome.run_id / "summary.md").read_text(encoding="utf-8"),
            sentinel,
        )
        verdict = read_json(outcome.verdict_path)
        self.assertEqual(verdict["status"], RunStatus.FAILED)
        self.assertIn(
            "SUITE_PATH_UNSAFE",
            {finding["code"] for finding in verdict["findings"]},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
