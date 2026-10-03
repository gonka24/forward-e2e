"""Evidence policy tests: selection-driven grading, expected artefacts, and verification rules.

Two defects are pinned here, each with its own negative fixture.

**1. The runner did not know what was selected.** Post-run provenance looked for
``live-context.json`` with a recursive glob and applied a single hardcoded
expectation to whatever it found. A boundary-only run -- which starts no chain
and deploys nothing -- was therefore failed for not producing deployment
evidence, while a native task that produced nothing could be rescued by another
task's context lying elsewhere in the same suite. The fix reads the selection
the planner recorded in ``lock.selection["proof_levels"]``
(:mod:`forward_e2e.execution.evidence`) and looks for each task's evidence in that task's
own run directory (``forward_e2e/execution/executor.py``: ``_evidence_requirements`` and
``_verify_post_run_provenance``).

**2. The collector never copied two artefacts the catalog declares mandatory.**
``scripts/run_go_boundary.py`` exports the buildx stage into
``<task evidence>/raw/``; the allowlist in ``forward_e2e/suite/collector.py`` only matched
``<task evidence>/go-boundary/raw/``. ``raw/go-test.json`` and ``raw/exit-code``
were consequently dropped, and ``forward_e2e/suite/reporter.py`` could only ever report
them missing -- a report that was right about the symptom and wrong about the
cause.

Every positive fixture reproduces the structure and the values the real
producer writes; every negative fixture changes or deletes exactly one
mandatory fact.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from forward_e2e.suite.catalog import (
    BOUNDARY_TASKS,
    get_task_by_id_or_alias,
    resolve_e2e_selection,
)
from forward_e2e.suite.collector import classify_artifact_kind, collect_snapshot_artifacts, update_artifact_index
from forward_e2e.execution.errors import BuildProvenanceError
from forward_e2e.execution.evidence import (
    EVIDENCE_MISSING,
    EVIDENCE_NOT_APPLICABLE,
    EVIDENCE_SATISFIED,
    LIVE_CONTEXT_FILENAME,
    SUITE_RUNS_DIRNAME,
    EvidencePolicyError,
    load_suite_plan,
    locate_task_evidence,
    lock_is_immutable_source,
    requirements_for,
    requirements_from_documents,
    selected_tasks_from_lock,
    selected_tasks_from_suite_plan,
    unclaimed_live_contexts,
)
from forward_e2e.execution.executor import (
    RestoredSource,
    _build_context,
    _evidence_requirements,
    _verify_post_run_provenance,
)
from forward_e2e.execution.outcome import RunStatus, evaluate_run
from forward_e2e.execution.planner import _selection_section
from forward_e2e.execution.runlock import (
    BuildManifest,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    ExecutionManifest,
    EXECUTION_MANIFEST_SCHEMA,
    ManifestStatus,
    RunLock,
    content_sha256,
    write_run_lock,
    write_delivery_manifest,
)
from forward_e2e.execution.runpackage import LoadedRunPackage
from forward_e2e.execution.sources import bundle_ref
from forward_e2e.suite.models import (
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
from forward_e2e.suite.runtime import RuntimeSnapshot
from forward_e2e.suite.verifier import verify_suite_artifacts_integrity

TEMP_PREFIX = "a8-test-e2e-r11ev-"

#: One run id, used as the suite id too -- the executor passes its run id to the
#: orchestrator as ``suite_id``, so the two are the same string in a real run.
SUITE_ID = "e2e-20260101-000000-r11ev"

REQUESTED_SHA = "a" * 40
PREPARED_SHA = "b" * 40
MARKETPLACE_SHA = "d" * 40
RUNNER_IMAGE_ID = "sha256:" + "1" * 64

DEAL_SHA256 = "2" * 64
FACTORY_SHA256 = "3" * 64
CALLER_SHA256 = "4" * 64
A9_MANIFEST_SHA256 = "6" * 64


# ---------------------------------------------------------------------------
from forward_e2e.suite.evidence_model import EVIDENCE_MODEL_IMMUTABLE
from tests.unit.runner.real_fixtures import real_lock_exact_e_context_for_run
from tests.unit.runner.support.fakes import (
    GONKA_TREE_SHA,
    IMMUTABLE_EVIDENCE_FILES,
    MARKETPLACE_TREE_SHA,
    StubLayout,
    apply_immutable_source_fields,
    live_context_payload as fake_live_context_payload,
    make_test_adapter,
    source_immutability_bytes,
)
from tests.unit.runner.support.packages import make_a9_build_manifest, make_baseline_lock


# ---------------------------------------------------------------------------
# fixtures built out of the real producers
# ---------------------------------------------------------------------------
def selection_section(*, profile=None, scenarios=None):
    """The ``lock.selection`` section the planner really writes."""
    tasks, resolved_profile, normalised = resolve_e2e_selection(
        profile=profile, scenarios=scenarios
    )
    return _selection_section(resolved_profile, normalised, tasks), tasks


def make_lock(selection) -> RunLock:
    """A complete, schema-valid run lock carrying the given selection."""
    return make_baseline_lock(
        plan_id="plan-r11ev",
        selection=selection,
        gonka_sha=REQUESTED_SHA,
        contracts_sha=MARKETPLACE_SHA,
        gonka_bundle_sha256="9" * 64,
        contracts_bundle_sha256="e" * 64,
        gonka_bundle_ref=bundle_ref("gonka"),
        contracts_bundle_ref=bundle_ref("contracts"),
        runner_image_id=RUNNER_IMAGE_ID,
    )


make_adapter = make_test_adapter


def make_manifest(root: Path, lock: RunLock, *, production_wasm: bool = True) -> BuildManifest:
    """A build manifest whose ``deployment`` section is filled by the real code."""
    manifest = make_a9_build_manifest(
        lock=lock,
        run_id=SUITE_ID,
        production_wasm=production_wasm,
        cw20_sha=None,
    )
    _build_context(
        lock=lock,
        layout=StubLayout(root / "runner"),
        gonka_adapter=make_adapter("gonka"),
        contracts_adapter=make_adapter("contracts"),
        gonka_src=RestoredSource(
            role="gonka",
            worktree=root / "src" / "gonka",
            commit_sha=REQUESTED_SHA,
            bundle_path=root / "bundles" / "gonka.bundle",
            origin="package",
        ),
        contracts_src=RestoredSource(
            role="contracts",
            worktree=root / "src" / "contracts",
            commit_sha=MARKETPLACE_SHA,
            bundle_path=root / "bundles" / "contracts.bundle",
            origin="package",
        ),
        runner_image_id=RUNNER_IMAGE_ID,
        manifest=manifest,
    )
    return manifest


def write_native_ownership(suite_dir, requirements, manifest):
    """Pair native task evidence with the image and cleanup records it owes."""
    from tests.unit.runner.support.fakes import ownership_payload

    if not manifest.images:
        manifest.images.append({
            "role": "gonka/inference-chain:e2e", "reference": "gonka/inference-chain:e2e",
            "image_id": RUNNER_IMAGE_ID, "repo_digests": [],
            "created_epoch": 1.0, "step_id": "chain-images",
        })
    for requirement in requirements:
        if requirement.requires_live_context:
            path = requirement.evidence_dir(suite_dir) / "cleanup-evidence" / "ownership.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ownership_payload(
                run_id=requirement.run_id, image_id=RUNNER_IMAGE_ID,
            )))


def live_context_payload(*, task_id: str, run_id: str) -> dict:
    """A ``live-context.json`` with the shape ``scripts/acceptance_harness.py`` writes."""
    return fake_live_context_payload(
        task_id=task_id,
        run_id=run_id,
        requested_sha=REQUESTED_SHA,
        observed_sha=REQUESTED_SHA,
        marketplace_sha=MARKETPLACE_SHA,
        a9_manifest_sha256=A9_MANIFEST_SHA256,
        a9_contract_sha256={"deal": DEAL_SHA256, "factory": FACTORY_SHA256},
        test_contract_sha256={"caller": CALLER_SHA256},
        evidence_model=EVIDENCE_MODEL_IMMUTABLE,
    )


def write_live_context(suite_dir: Path, run_id: str, payload) -> Path:
    """Mirror the harness's nested output, preserved by the collector."""
    path = (
        Path(suite_dir) / SUITE_RUNS_DIRNAME / run_id
        / "evidence" / run_id / LIVE_CONTEXT_FILENAME
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "source-immutability.json").write_bytes(
        source_immutability_bytes(
            gonka_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        )
    )
    for rel_path, content in IMMUTABLE_EVIDENCE_FILES.items():
        target = path.parent / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def suite_source_identity() -> SourceIdentity:
    return SourceIdentity(
        marketplace_commit_sha=MARKETPLACE_SHA,
        gonka_commit_sha=REQUESTED_SHA,
        runner_version_hash="7" * 64,
        catalog_version_hash="8" * 64,
        gonka_tree_sha=GONKA_TREE_SHA,
        marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        source_immutability_verdict="UNCHANGED",
    )


def suite_plan_for(tasks, *, suite_id: str = SUITE_ID, profile=None) -> SuitePlan:
    """The ``suite-plan.json`` document the orchestrator writes for a selection."""
    return SuitePlan(
        schema_version="1.0.0",
        suite_id=suite_id,
        created_at_utc="2026-01-01T00:00:00+00:00",
        profile=profile,
        requested_scenarios=[task.task_id for task in tasks],
        source_identity=suite_source_identity(),
        tasks=[TaskPlan.from_dict(task.to_dict()) for task in tasks],
    )


def suite_result_for(tasks, *, suite_id: str = SUITE_ID) -> SuiteResult:
    """A green ``suite-result.json``: the state F1/F3 are about."""
    return SuiteResult(
        schema_version="1.0.0",
        suite_id=suite_id,
        created_at_utc="2026-01-01T00:00:00+00:00",
        completed_at_utc="2026-01-01T01:00:00+00:00",
        source_identity=suite_source_identity(),
        overall_status=ExecutionStatus.PASSED,
        tasks=[
            TaskResult(
                task_id=task.task_id,
                ordinal=task.ordinal,
                run_id=make_task_run_id(suite_id, task.ordinal, task.task_id),
                proof_level=task.proof_level,
                execution_status=ExecutionStatus.PASSED,
                evidence_status=EvidenceStatus.COMPLETE,
                cleanup_status=CleanupStatus.CLEANED,
                acceptance_status=AcceptanceStatus.NOT_REVIEWED,
                start_time_utc="2026-01-01T00:00:00+00:00",
                end_time_utc="2026-01-01T01:00:00+00:00",
                duration_seconds=3600.0,
                phase="COMPLETED",
                exit_code=0,
                primary_failure=None,
                secondary_errors=[],
                expected_cases=list(task.expected_checkpoints),
                observed_passed_cases=list(task.expected_checkpoints),
                missing_cases=[],
                missing_artifacts=[],
                raw_evidence_dir=None,
                exported_evidence_dir=None,
            )
            for task in tasks
        ],
        summary_message="All selected tasks passed",
    )


# ---------------------------------------------------------------------------
# 1. the selection really does name each task's own run id
# ---------------------------------------------------------------------------
class SelectionDerivesPerTaskRunIdsTests(unittest.TestCase):
    """``selected_tasks_from_lock`` / ``selected_tasks_from_suite_plan``.

    Exercises ``forward_e2e/execution/evidence.py``: ``selected_tasks_from_lock`` and
    ``selected_tasks_from_suite_plan``.
    """

    def test_a_mixed_lock_selection_yields_each_tasks_own_catalog_ordered_run_id(self):
        """Boundary tasks come first because the catalog says so, not the CLI."""
        selection, tasks = selection_section(
            scenarios=["lock-exact-e", "go-boundary", "funded-claim"]
        )
        lock = make_lock(selection)

        selected = selected_tasks_from_lock(lock)

        self.assertEqual(
            [(task.task_id, task.ordinal, task.proof_level) for task in selected],
            [
                ("go-query-error-classification", 1, ProofLevel.GO_BOUNDARY.value),
                ("funded-claim", 2, ProofLevel.NATIVE.value),
                ("lock-exact-e", 3, ProofLevel.NATIVE.value),
            ],
        )
        # The run ids are the ones the orchestrator will really create, derived
        # by the production helper rather than re-spelled here.
        self.assertEqual(
            [task.run_id(SUITE_ID) for task in selected],
            [
                make_task_run_id(SUITE_ID, 1, "go-query-error-classification"),
                make_task_run_id(SUITE_ID, 2, "funded-claim"),
                make_task_run_id(SUITE_ID, 3, "lock-exact-e"),
            ],
        )
        # ...and they agree with what the resolver itself assigned.
        self.assertEqual(
            [task.run_id(SUITE_ID) for task in selected],
            [make_task_run_id(SUITE_ID, t.ordinal, t.task_id) for t in tasks],
        )

    def test_the_run_id_of_a_task_is_not_the_one_its_position_on_the_command_line_suggests(self):
        """Negative case: the order typed by the operator is not the run order.

        If a reader derived ordinals from the requested order it would look for
        ``-01-lock-exact-e`` and find nothing, which is precisely the class of
        bug that made the runner unable to say what a task owed.
        """
        requested = ["lock-exact-e", "go-boundary", "funded-claim", "lock-exact-e"]
        selection, _ = selection_section(scenarios=requested)

        run_ids = [task.run_id(SUITE_ID) for task in selected_tasks_from_lock(make_lock(selection))]

        self.assertEqual(requested[0], "lock-exact-e")
        self.assertEqual(
            selection["requested_scenarios"],
            ["go-query-error-classification", "funded-claim", "lock-exact-e"],
        )
        self.assertNotIn(make_task_run_id(SUITE_ID, 1, "lock-exact-e"), run_ids)
        self.assertIn(make_task_run_id(SUITE_ID, 3, "lock-exact-e"), run_ids)

    def test_a_boundary_profile_and_naming_every_boundary_scenario_resolve_to_the_same_run_ids(self):
        """A profile is a shorthand for a scenario list, never a different list."""
        profile_selection, _ = selection_section(profile="boundary")
        scrambled = [task.task_id for task in reversed(BOUNDARY_TASKS)]
        explicit_selection, _ = selection_section(scenarios=scrambled)

        from_profile = selected_tasks_from_lock(make_lock(profile_selection))
        from_scenarios = selected_tasks_from_lock(make_lock(explicit_selection))

        self.assertEqual(
            [(t.task_id, t.ordinal, t.proof_level) for t in from_profile],
            [(t.task_id, t.ordinal, t.proof_level) for t in from_scenarios],
        )
        self.assertEqual(
            [t.run_id(SUITE_ID) for t in from_profile],
            [t.run_id(SUITE_ID) for t in from_scenarios],
        )
        # Every boundary task of the real catalog is covered, in catalog order.
        self.assertEqual(
            [t.task_id for t in from_profile], [t.task_id for t in BOUNDARY_TASKS]
        )

    def test_a_suite_plan_derives_the_run_ids_the_suite_itself_used(self):
        """The producer's own record wins for *locating* evidence."""
        _, tasks = selection_section(scenarios=["go-boundary", "lock-exact-e"])
        plan = suite_plan_for(tasks)

        selected = selected_tasks_from_suite_plan(plan.to_dict())

        self.assertEqual(
            [(t.task_id, t.ordinal, t.proof_level) for t in selected],
            [
                ("go-query-error-classification", 1, ProofLevel.GO_BOUNDARY.value),
                ("lock-exact-e", 2, ProofLevel.NATIVE.value),
            ],
        )
        self.assertEqual(
            [t.run_id(SUITE_ID) for t in selected],
            [
                make_task_run_id(SUITE_ID, 1, "go-query-error-classification"),
                make_task_run_id(SUITE_ID, 2, "lock-exact-e"),
            ],
        )

    def test_a_suite_plan_that_ran_a_different_set_than_the_lock_selected_is_reported(self):
        """Negative case: the plan ran one task fewer than the lock selected.

        The evidence is still located by the ordinals the suite really used,
        but the disagreement itself is a finding: a suite that quietly ran
        something else must not be graded against the set nobody ran.
        """
        lock_selection, _ = selection_section(scenarios=["go-boundary", "lock-exact-e"])
        _, planned_tasks = selection_section(scenarios=["lock-exact-e"])

        requirements, disagreements = requirements_from_documents(
            lock=make_lock(lock_selection),
            suite_id=SUITE_ID,
            build_produced_production_wasm=True,
            suite_plan=suite_plan_for(planned_tasks).to_dict(),
        )

        self.assertEqual([item["code"] for item in disagreements], ["SELECTION_DISAGREEMENT"])
        self.assertEqual(
            disagreements[0]["lock_selection"],
            ["go-query-error-classification", "lock-exact-e"],
        )
        self.assertEqual(disagreements[0]["suite_plan_selection"], ["lock-exact-e"])
        # The suite really used ordinal 1 for the only task it ran.
        self.assertEqual(
            [req.run_id for req in requirements],
            [make_task_run_id(SUITE_ID, 1, "lock-exact-e")],
        )


# ---------------------------------------------------------------------------
# 2. a selection that cannot be read is an error, never "no requirements"
# ---------------------------------------------------------------------------
class UnreadableSelectionIsAHardErrorTests(unittest.TestCase):
    """The "check that disappears" case.

    Exercises ``forward_e2e/execution/evidence.py``: ``_proof_level_of``,
    ``selected_tasks_from_lock``, ``selected_tasks_from_suite_plan``; and
    ``forward_e2e/execution/executor.py``: ``_evidence_requirements``.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_unknown_provenance_model_keeps_source_immutability_required(self):
        class FutureLock:
            provenance_model = "future-source-model"

        self.assertTrue(lock_is_immutable_source(FutureLock()))

        FutureLock.provenance_model = "historical-prepared-build"
        self.assertFalse(lock_is_immutable_source(FutureLock()))

    def test_a_suite_plan_with_an_invalid_ordinal_reports_a_policy_error_without_coercion(self):
        _, tasks = selection_section(scenarios=["lock-exact-e"])
        for ordinal in ("oops", "1", None, 0, -1, 1.5, 1.0, True, [], {}):
            with self.subTest(ordinal=ordinal):
                payload = suite_plan_for(tasks).to_dict()
                payload["tasks"][0]["ordinal"] = ordinal
                with self.assertRaisesRegex(EvidencePolicyError, "positive integer"):
                    selected_tasks_from_suite_plan(payload)

    def test_a_suite_plan_without_an_ordinal_does_not_invent_a_zero_run_directory(self):
        _, tasks = selection_section(scenarios=["lock-exact-e"])
        payload = suite_plan_for(tasks).to_dict()
        del payload["tasks"][0]["ordinal"]
        with self.assertRaisesRegex(EvidencePolicyError, "positive integer"):
            selected_tasks_from_suite_plan(payload)

    def test_a_suite_plan_with_corrupted_encoding_reports_an_unreadable_policy_document(self):
        _, tasks = selection_section(scenarios=["lock-exact-e"])
        path = self.root / "suite-plan.json"
        content = json.dumps(suite_plan_for(tasks).to_dict()).encode("utf-8")
        path.write_bytes(content)
        self.assertEqual(load_suite_plan(self.root)["tasks"][0]["ordinal"], 1)
        # Change one byte of the producer-shaped document to invalid UTF-8.
        path.write_bytes(content.replace(b"lock-exact-e", b"lock-exact-\xff", 1))
        with self.assertRaisesRegex(EvidencePolicyError, "unreadable"):
            load_suite_plan(self.root)

    def test_a_selection_recording_an_unknown_proof_level_raises_instead_of_demanding_nothing(self):
        """Negative case: one proof level is a name this runner does not know.

        Everything else in the selection is exactly what the planner writes.
        A runner that shrugged here would demand nothing of that task and grade
        the run green on no evidence at all.
        """
        selection, _ = selection_section(scenarios=["go-boundary", "lock-exact-e"])
        selection["proof_levels"]["lock-exact-e"] = "NATIVE_LITE"

        with self.assertRaises(EvidencePolicyError) as caught:
            selected_tasks_from_lock(make_lock(selection))

        self.assertIn("NATIVE_LITE", str(caught.exception))
        self.assertIn("cannot decide", str(caught.exception))

    def test_a_selection_that_records_no_proof_level_for_a_selected_task_raises(self):
        """Negative case: the proof level of one selected task is simply absent."""
        selection, _ = selection_section(scenarios=["go-boundary", "lock-exact-e"])
        del selection["proof_levels"]["lock-exact-e"]

        with self.assertRaises(EvidencePolicyError) as caught:
            selected_tasks_from_lock(make_lock(selection))

        self.assertIn("lock-exact-e", str(caught.exception))
        self.assertIn("records no proof level", str(caught.exception))

    def test_a_suite_plan_task_entry_with_an_unknown_proof_level_raises(self):
        """Negative case: the same corruption, in the producer's own document."""
        _, tasks = selection_section(scenarios=["go-boundary"])
        payload = suite_plan_for(tasks).to_dict()
        payload["tasks"][0]["proof_level"] = "GO_BOUNDARY_LITE"

        with self.assertRaises(EvidencePolicyError) as caught:
            selected_tasks_from_suite_plan(payload)

        self.assertIn("GO_BOUNDARY_LITE", str(caught.exception))

    def test_the_executor_turns_an_unreadable_selection_into_a_blocking_failure(self):
        """The production wrapper must fail the run, not skip the policy."""
        selection, _ = selection_section(scenarios=["lock-exact-e"])
        selection["proof_levels"]["lock-exact-e"] = "NATIVE_LITE"
        lock = make_lock(selection)
        manifest = make_manifest(self.root, lock)
        suite_dir = self.root / "suite" / SUITE_ID
        suite_dir.mkdir(parents=True)

        with self.assertRaises(BuildProvenanceError) as caught:
            _evidence_requirements(
                lock=lock, suite_id=SUITE_ID, suite_dir=suite_dir, manifest=manifest
            )

        self.assertIn("could not be determined from its own selection", str(caught.exception))
        self.assertIn("NATIVE_LITE", str(caught.exception))


# ---------------------------------------------------------------------------
# 3. per proof level, the right evidence and nothing else
# ---------------------------------------------------------------------------
class PerProofLevelRequirementsTests(unittest.TestCase):
    """Exercises ``forward_e2e/execution/evidence.py``: ``requirements_for``,
    ``locate_task_evidence``; and ``forward_e2e/execution/executor.py``:
    ``_verify_post_run_provenance``.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name)
        self.suite_dir = self.root / "suite" / SUITE_ID
        self.suite_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def post_run(self, manifest, requirements, emitted=None):
        return _verify_post_run_provenance(
            self.suite_dir,
            manifest=manifest,
            adapter=make_adapter("gonka"),
            sources_root=self.root / "src" / "gonka",
            selected_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            requirements=requirements,
            emit=(emitted.append if emitted is not None else (lambda message: None)),
        )

    def test_a_native_task_must_produce_both_a_live_context_and_deployment_evidence(self):
        selection, _ = selection_section(scenarios=["lock-exact-e"])
        lock = make_lock(selection)
        manifest = make_manifest(self.root, lock)

        requirements, _ = _evidence_requirements(
            lock=lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )

        self.assertEqual(len(requirements), 1)
        requirement = requirements[0]
        self.assertEqual(requirement.proof_level, ProofLevel.NATIVE.value)
        self.assertTrue(requirement.requires_live_context)
        self.assertTrue(requirement.requires_deployment_evidence)
        self.assertTrue(requirement.requires_source_immutability)
        # The declared artefacts come from the real catalog entry.
        self.assertEqual(
            list(requirement.expected_artifacts),
            list(get_task_by_id_or_alias("lock-exact-e").expected_artifacts),
        )
        self.assertIn(LIVE_CONTEXT_FILENAME, requirement.expected_artifacts)

    def test_no_boundary_proof_level_is_ever_asked_for_a_live_context_or_a_deployment(self):
        """Every GO_BOUNDARY / WASM_ABI / CONTRACT_TEST task of the real catalog."""
        selection, _ = selection_section(profile="boundary")
        lock = make_lock(selection)
        # The contracts recipe fills production_wasm regardless of selection;
        # that must not create a duty for a task that deploys nothing.
        manifest = make_manifest(self.root, lock, production_wasm=True)
        self.assertTrue(manifest.deployment["production_wasm"])

        requirements, _ = _evidence_requirements(
            lock=lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )

        self.assertEqual(
            sorted({req.proof_level for req in requirements}),
            sorted(
                {
                    ProofLevel.GO_BOUNDARY.value,
                    ProofLevel.WASM_ABI.value,
                    ProofLevel.CONTRACT_TEST.value,
                }
            ),
        )
        for requirement in requirements:
            with self.subTest(task=requirement.task_id):
                self.assertFalse(requirement.requires_live_context)
                self.assertFalse(requirement.requires_deployment_evidence)
                self.assertFalse(requirement.requires_source_immutability)

    def test_a_boundary_only_run_that_wrote_no_live_context_is_not_failed_for_its_absence(self):
        """Negative case: the suite directory contains no live context at all.

        This is the exact state that used to raise ``BuildProvenanceError`` for
        a successful ``--profile boundary`` run.
        """
        selection, _ = selection_section(profile="boundary")
        lock = make_lock(selection)
        manifest = make_manifest(self.root, lock)
        requirements, _ = _evidence_requirements(
            lock=lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )
        emitted: list = []

        findings = self.post_run(manifest, requirements, emitted=emitted)

        self.assertEqual(findings["missing_evidence"], [])
        self.assertEqual(findings["live_contexts"], [])
        self.assertEqual(findings["deployment"], [])
        self.assertEqual(len(findings["policy"]), len(BOUNDARY_TASKS))
        self.assertTrue(
            any("owes no live-context" in line for line in emitted),
            emitted,
        )
        # Nothing is silently skipped: each task is recorded as not applicable.
        located = locate_task_evidence(requirements, suite_dir=self.suite_dir)
        self.assertEqual(
            {item.status for item in located}, {EVIDENCE_NOT_APPLICABLE}
        )
        self.assertEqual([item.problems for item in located], [[] for _ in located])

    def test_a_single_boundary_task_owes_nothing_a_native_task_in_the_same_state_owes_a_context(self):
        """The contrast that proves the rule is keyed on the proof level.

        One fact differs between the two runs: which task was selected. The
        suite directory is empty in both.
        """
        boundary_selection, _ = selection_section(scenarios=["wasm-abi-boundary"])
        native_selection, _ = selection_section(scenarios=["lock-exact-e"])
        boundary_lock = make_lock(boundary_selection)
        native_lock = make_lock(native_selection)
        manifest = make_manifest(self.root, boundary_lock)

        boundary_requirements, _ = _evidence_requirements(
            lock=boundary_lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )
        native_requirements, _ = _evidence_requirements(
            lock=native_lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )

        self.assertEqual(self.post_run(manifest, boundary_requirements)["missing_evidence"], [])
        native_missing = self.post_run(manifest, native_requirements)["missing_evidence"]
        self.assertEqual([entry["task_id"] for entry in native_missing], ["lock-exact-e", "lock-exact-e"])
        self.assertTrue(native_missing[1]["expected_path"].endswith("cleanup-evidence/ownership.json"))
        self.assertEqual(
            native_missing[0]["expected_path"],
            str(
                Path(SUITE_RUNS_DIRNAME)
                / make_task_run_id(SUITE_ID, 1, "lock-exact-e")
                / "evidence"
                / make_task_run_id(SUITE_ID, 1, "lock-exact-e")
                / LIVE_CONTEXT_FILENAME
            ),
        )

    def test_a_native_task_still_owes_its_live_context_when_the_build_made_no_production_wasm(self):
        """Negative case: the build produced only harness fixtures.

        The build-side fact may switch the *deployment* comparison off -- there
        is nothing to compare against -- but it can never remove the duty to
        say which runtime was observed.
        """
        selection, _ = selection_section(scenarios=["lock-exact-e"])
        lock = make_lock(selection)
        manifest = make_manifest(self.root, lock, production_wasm=False)
        self.assertEqual(manifest.deployment["production_wasm"], [])

        requirements, _ = _evidence_requirements(
            lock=lock, suite_id=SUITE_ID, suite_dir=self.suite_dir, manifest=manifest
        )

        self.assertTrue(requirements[0].requires_live_context)
        self.assertFalse(requirements[0].requires_deployment_evidence)

    def test_a_boundary_task_that_did_write_a_live_context_is_recorded_rather_than_ignored(self):
        """An unexpected artefact is reported, not quietly treated as proof."""
        selection, _ = selection_section(scenarios=["go-boundary"])
        lock = make_lock(selection)
        requirements = requirements_for(
            selected_tasks_from_lock(lock),
            suite_id=SUITE_ID,
            build_produced_production_wasm=True,
        )
        write_live_context(
            self.suite_dir,
            requirements[0].run_id,
            live_context_payload(task_id="go-boundary", run_id=requirements[0].run_id),
        )

        located = locate_task_evidence(requirements, suite_dir=self.suite_dir)

        self.assertEqual(located[0].status, EVIDENCE_SATISFIED)
        self.assertTrue(located[0].live_context_present)
        # It is claimed by the task that produced it, so it is not "unclaimed".
        self.assertEqual(unclaimed_live_contexts(requirements, suite_dir=self.suite_dir), [])


# ---------------------------------------------------------------------------
# 4. each task's evidence is read from that task's own directory
# ---------------------------------------------------------------------------
class EvidenceIsReadFromTheTasksOwnDirectoryTests(unittest.TestCase):
    """Exercises ``forward_e2e/execution/executor.py``: ``_verify_post_run_provenance``
    and ``forward_e2e/execution/evidence.py``: ``locate_task_evidence``.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name)
        self.suite_dir = self.root / "suite" / SUITE_ID
        self.suite_dir.mkdir(parents=True)
        self.selection, self.tasks = selection_section(
            scenarios=["funded-claim", "lock-exact-e"]
        )
        self.lock = make_lock(self.selection)
        self.manifest = make_manifest(self.root, self.lock)
        self.requirements, _ = _evidence_requirements(
            lock=self.lock,
            suite_id=SUITE_ID,
            suite_dir=self.suite_dir,
            manifest=self.manifest,
        )
        self.by_task = {req.task_id: req for req in self.requirements}
        write_native_ownership(self.suite_dir, self.requirements, self.manifest)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_live_context_with_corrupted_encoding_is_reported_missing_instead_of_raising(self):
        requirement = self.by_task["lock-exact-e"]
        path = write_live_context(
            self.suite_dir, requirement.run_id,
            live_context_payload(task_id=requirement.task_id, run_id=requirement.run_id),
        )
        self.assertEqual(
            locate_task_evidence([requirement], suite_dir=self.suite_dir)[0].status,
            EVIDENCE_SATISFIED,
        )
        content = path.read_bytes()
        path.write_bytes(content.replace(b"source", b"sourc\xff", 1))
        evidence = locate_task_evidence([requirement], suite_dir=self.suite_dir)[0]
        self.assertEqual(evidence.status, EVIDENCE_MISSING)
        self.assertEqual(evidence.problems[0]["code"], "DOCUMENT_UNREADABLE")

    def test_a_missing_live_context_points_to_the_actual_harness_output_directory(self):
        requirement = self.by_task["lock-exact-e"]
        path = (
            self.suite_dir / "runs" / requirement.run_id
            / "evidence" / requirement.run_id / "live-context.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(live_context_payload(
            task_id=requirement.task_id, run_id=requirement.run_id,
        )), encoding="utf-8")
        self.assertEqual(requirement.live_context_path(self.suite_dir), path)
        path.unlink()
        evidence = locate_task_evidence([requirement], suite_dir=self.suite_dir)[0]
        self.assertEqual(evidence.status, EVIDENCE_MISSING)
        self.assertEqual(evidence.problems[0]["expected_path"], str(path.relative_to(self.suite_dir)))

    def test_compatibility_layouts_preserve_task_evidence_when_the_canonical_file_is_absent(self):
        requirement = self.by_task["lock-exact-e"]
        run_dir = self.suite_dir / "runs" / requirement.run_id
        for relative in ("evidence/live-context.json", "live-context.json"):
            with self.subTest(layout=relative):
                canonical = write_live_context(
                    self.suite_dir, requirement.run_id,
                    live_context_payload(task_id=requirement.task_id, run_id=requirement.run_id),
                )
                canonical_immut = canonical.parent / "source-immutability.json"
                destination = run_dir / relative
                destination_immut = destination.parent / "source-immutability.json"
                canonical.rename(destination)
                canonical_immut.rename(destination_immut)
                evidence = locate_task_evidence([requirement], suite_dir=self.suite_dir)[0]
                self.assertEqual(evidence.status, EVIDENCE_SATISFIED)
                self.assertEqual(evidence.problems, [])
                self.assertEqual(
                    evidence.live_context_path, str(destination.relative_to(self.suite_dir))
                )
                self.assertEqual(
                    unclaimed_live_contexts(self.requirements, suite_dir=self.suite_dir), []
                )
                destination.unlink()
                destination_immut.unlink()

    def post_run(self):
        return _verify_post_run_provenance(
            self.suite_dir,
            manifest=self.manifest,
            adapter=make_adapter("gonka"),
            sources_root=self.root / "src" / "gonka",
            selected_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            requirements=self.requirements,
            emit=lambda message: None,
        )

    def test_each_selected_native_task_is_read_from_its_own_numbered_run_directory(self):
        for task_id, requirement in self.by_task.items():
            write_live_context(
                self.suite_dir,
                requirement.run_id,
                live_context_payload(task_id=task_id, run_id=requirement.run_id),
            )

        findings = self.post_run()

        self.assertEqual(findings["missing_evidence"], [])
        self.assertEqual(
            sorted(entry["path"] for entry in findings["live_contexts"]),
            sorted(
                str(
                    Path(SUITE_RUNS_DIRNAME) / req.run_id
                    / "evidence" / req.run_id / LIVE_CONTEXT_FILENAME
                )
                for req in self.requirements
            ),
        )
        self.assertEqual(
            sorted(entry["task_id"] for entry in findings["live_contexts"]),
            ["funded-claim", "lock-exact-e"],
        )
        self.assertEqual(
            {entry["status"] for entry in findings["deployment"]}, {"MATCHED"}
        )

    def test_a_native_task_missing_source_immutability_json_is_reported_missing(self):
        """A native task that wrote live-context.json without source-immutability.json fails."""
        for task_id, requirement in self.by_task.items():
            write_live_context(
                self.suite_dir,
                requirement.run_id,
                live_context_payload(task_id=task_id, run_id=requirement.run_id),
            )
        target_req = self.by_task["lock-exact-e"]
        immut_path = target_req.source_immutability_path(self.suite_dir)
        self.assertTrue(immut_path.is_file())
        immut_path.unlink()

        located = {
            item.requirement.task_id: item
            for item in locate_task_evidence(self.requirements, suite_dir=self.suite_dir)
        }
        self.assertEqual(located["funded-claim"].status, EVIDENCE_SATISFIED)
        self.assertEqual(located["lock-exact-e"].status, EVIDENCE_MISSING)
        self.assertFalse(located["lock-exact-e"].source_immutability_present)
        self.assertIn(
            "SOURCE_IMMUTABILITY_MISSING",
            [problem["code"] for problem in located["lock-exact-e"].problems],
        )

        findings = self.post_run()
        self.assertEqual(
            [entry["task_id"] for entry in findings["missing_evidence"]],
            ["lock-exact-e"],
        )
        self.assertEqual(
            findings["missing_evidence"][0]["reason"],
            "source immutability evidence is incomplete: source-immutability.json",
        )

    def test_another_tasks_live_context_does_not_satisfy_the_selected_task(self):
        """Negative case: only the *other* selected task wrote its context.

        A recursive search would find a perfectly valid ``live-context.json``
        in this suite and pass. The check must still fail the task that left
        nothing, and must not let the neighbour's evidence stand in for it.
        """
        donor = self.by_task["funded-claim"]
        write_live_context(
            self.suite_dir,
            donor.run_id,
            live_context_payload(task_id="funded-claim", run_id=donor.run_id),
        )
        # A recursive glob -- the old implementation -- would have been happy.
        self.assertEqual(len(list(self.suite_dir.rglob(LIVE_CONTEXT_FILENAME))), 1)

        findings = self.post_run()

        self.assertEqual([entry["task_id"] for entry in findings["missing_evidence"]], ["lock-exact-e"])
        self.assertEqual(
            findings["missing_evidence"][0]["run_id"], self.by_task["lock-exact-e"].run_id
        )
        self.assertEqual(
            findings["missing_evidence"][0]["expected_path"],
            str(
                Path(SUITE_RUNS_DIRNAME)
                / self.by_task["lock-exact-e"].run_id
                / "evidence"
                / self.by_task["lock-exact-e"].run_id
                / LIVE_CONTEXT_FILENAME
            ),
        )
        # The one context that does exist is credited to its own task only.
        self.assertEqual([entry["task_id"] for entry in findings["live_contexts"]], ["funded-claim"])
        located = {item.requirement.task_id: item for item in
                   locate_task_evidence(self.requirements, suite_dir=self.suite_dir)}
        self.assertEqual(located["funded-claim"].status, EVIDENCE_SATISFIED)
        self.assertEqual(located["lock-exact-e"].status, EVIDENCE_MISSING)
        self.assertEqual(
            [problem["code"] for problem in located["lock-exact-e"].problems],
            ["LIVE_CONTEXT_MISSING", "SOURCE_IMMUTABILITY_MISSING"],
        )

    def test_a_context_in_a_directory_with_the_right_task_and_the_wrong_ordinal_is_not_accepted(self):
        """Negative case: one changed fact -- the ordinal in the directory name.

        The file, its contents and the task name are all correct; only the
        directory the task really owns is empty.
        """
        paths = {}
        for task_id, requirement in self.by_task.items():
            paths[task_id] = write_live_context(
                self.suite_dir, requirement.run_id,
                live_context_payload(task_id=task_id, run_id=requirement.run_id),
            )
        requirement = self.by_task["lock-exact-e"]
        wrong_ordinal = make_task_run_id(SUITE_ID, 9, "lock-exact-e")
        self.assertNotEqual(wrong_ordinal, requirement.run_id)
        misplaced = (
            self.suite_dir / SUITE_RUNS_DIRNAME / wrong_ordinal
            / "evidence" / requirement.run_id / LIVE_CONTEXT_FILENAME
        )
        misplaced.parent.mkdir(parents=True)
        paths["lock-exact-e"].rename(misplaced)

        findings = self.post_run()

        missing = {entry["task_id"] for entry in findings["missing_evidence"]}
        self.assertEqual(missing, {"lock-exact-e"})
        self.assertEqual(
            [entry["task_id"] for entry in findings["live_contexts"]], ["funded-claim"]
        )
        # The stray context belongs to nobody and is reported as such.
        self.assertEqual(
            unclaimed_live_contexts(self.requirements, suite_dir=self.suite_dir),
            [str(misplaced.relative_to(self.suite_dir))],
        )


# ---------------------------------------------------------------------------
# 5. the missing context travels from the run into the offline verdict
# ---------------------------------------------------------------------------
class MissingNativeEvidenceReachesTheRunVerdictTests(unittest.TestCase):
    """The full executor -> manifest -> run package -> verdict path.

    Exercises ``forward_e2e/execution/executor.py``: ``_evidence_requirements`` and
    ``_verify_post_run_provenance``; ``forward_e2e/execution/runpackage.py``:
    ``LoadedRunPackage.load`` / ``provenance_findings`` / ``task_evidence``;
    and ``forward_e2e/execution/outcome.py``: ``evaluate_run``.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def build_package(self, *, write_context: bool) -> Path:
        """Write a full run package the way the executor writes one.

        ``write_context`` is the single fact that differs between the positive
        and the negative fixture: whether the selected native task left its
        ``live-context.json`` in its own run directory.
        """
        run_dir = self.root / "output" / SUITE_ID
        suite_dir = run_dir / "suite" / SUITE_ID
        suite_dir.mkdir(parents=True)

        selection, tasks = selection_section(scenarios=["lock-exact-e"])
        lock = make_lock(selection)
        write_run_lock(lock, run_dir / "run.lock.json")

        plan = suite_plan_for(tasks)
        (suite_dir / "suite-plan.json").write_text(
            json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        (suite_dir / "suite-result.json").write_text(
            json.dumps(suite_result_for(tasks).to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

        manifest = make_manifest(self.root, lock)
        manifest.finish(required_roles=())
        self.assertEqual(manifest.status, ManifestStatus.COMPLETE)

        requirements, disagreements = _evidence_requirements(
            lock=lock, suite_id=SUITE_ID, suite_dir=suite_dir, manifest=manifest
        )
        write_native_ownership(suite_dir, requirements, manifest)
        task_run_id = requirements[0].run_id
        task_run_dir = suite_dir / "runs" / task_run_id
        task_run_dir.mkdir(parents=True, exist_ok=True)
        ident_data = {
            "run_id": task_run_id,
            "task_id": "lock-exact-e",
            "marketplace_commit_sha": MARKETPLACE_SHA,
            "marketplace_source_sha": MARKETPLACE_SHA,
            "marketplace_tree_sha": MARKETPLACE_TREE_SHA,
            "gonka_commit_sha": REQUESTED_SHA,
            "gonka_source_sha": REQUESTED_SHA,
            "gonka_tree_sha": GONKA_TREE_SHA,
            "source_immutability_verdict": "UNCHANGED",
            "evidence_model": EVIDENCE_MODEL_IMMUTABLE,
        }
        ident_file = task_run_dir / "identity.json"
        ident_file.write_text(json.dumps(ident_data, indent=2) + "\n", encoding="utf-8")

        junit_file = task_run_dir / "junit" / "TEST-MarketplaceContractAcceptanceTests.xml"
        junit_file.parent.mkdir(parents=True, exist_ok=True)
        junit_file.write_text(
            "<testsuite name='MarketplaceContractAcceptanceTests' tests='1' failures='0' errors='0'>\n"
            "  <testcase name='marketplace funded lock succeeds exactly at E' classname='MarketplaceContractAcceptanceTests'/>\n"
            "</testsuite>\n",
            encoding="utf-8",
        )
        log_file = task_run_dir / "testermint.log"
        log_file.write_text("testermint log\n", encoding="utf-8")

        if write_context:
            ctx = real_lock_exact_e_context_for_run(task_run_id)
            apply_immutable_source_fields(
                ctx,
                gonka_sha=REQUESTED_SHA,
                marketplace_sha=MARKETPLACE_SHA,
                gonka_tree_sha=GONKA_TREE_SHA,
                marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            )
            ctx["source"]["gonka_source_sha"] = REQUESTED_SHA
            ctx["source"]["a9_manifest_sha256"] = A9_MANIFEST_SHA256
            ctx["source"]["a9_contract_sha256"] = {"deal": DEAL_SHA256, "factory": FACTORY_SHA256}
            ctx["source"]["test_contract_sha256"] = {"caller": CALLER_SHA256}
            if "runtime" in ctx["source"]:
                ctx["source"]["runtime"]["gonka_source_sha"] = REQUESTED_SHA
            write_live_context(suite_dir, task_run_id, ctx)

        artifacts = [
            {
                "relative_path": path.relative_to(suite_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "run_id": task_run_id,
                "task_id": "lock-exact-e",
                "artifact_kind": classify_artifact_kind(path.relative_to(task_run_dir).as_posix()),
            }
            for path in sorted(
                (p for p in task_run_dir.rglob("*") if p.is_file()),
                key=lambda p: p.relative_to(suite_dir).as_posix(),
            )
        ]
        (suite_dir / "artifact-index.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "total_artifacts": len(artifacts),
                    "artifacts": artifacts,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        findings = _verify_post_run_provenance(
            suite_dir,
            manifest=manifest,
            adapter=make_adapter("gonka"),
            sources_root=self.root / "src" / "gonka",
            selected_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            requirements=requirements,
            emit=lambda message: None,
        )
        findings["selection_disagreements"] = disagreements
        manifest.observed_runtime = findings
        manifest.write(run_dir / "build-manifest.json")

        execution = ExecutionManifest(
            schema_version=EXECUTION_MANIFEST_SCHEMA,
            lock_sha256=lock.lock_sha256,
            plan_id=lock.plan_id,
            run_id=SUITE_ID,
            suite_id=SUITE_ID,
            created_at_utc="2026-01-01T00:00:00+00:00",
            command="run",
            build_manifest_sha256=content_sha256(manifest.to_dict()),
            suite_export_relpath=str(Path("suite") / SUITE_ID),
            artifact_index_relpath=str(Path("suite") / SUITE_ID / "artifact-index.json"),
            source_immutability={
                "verdict": manifest.source_immutability["verdict"],
                "after_execution": {
                    role: dict(manifest.source_immutability["roles"][role]["after_execution"])
                    for role in ("gonka", "contracts")
                },
            },
        )
        execution.write(run_dir / "execution-manifest.json")
        write_delivery_manifest(
            DeliveryManifest(
                run_id=SUITE_ID,
                status=DeliveryStatus.COMPLETED,
                attempts=[DeliveryAttempt(
                    attempt_number=1,
                    command="run",
                    started_at_utc="2026-01-01T00:00:00+00:00",
                    completed_at_utc="2026-01-01T00:00:01+00:00",
                    destination=str(run_dir),
                    status=DeliveryStatus.COMPLETED,
                    error=None,
                )],
            ),
            run_dir / "delivery.json",
        )
        return run_dir

    def test_a_native_task_that_left_its_context_is_graded_passed(self):
        """The positive fixture, so the negative one differs by exactly one file."""
        run_dir = self.build_package(write_context=True)

        outcome = evaluate_run(LoadedRunPackage.load(run_dir))

        codes = [finding.code for finding in outcome.findings]
        self.assertNotIn("TASK_EVIDENCE_MISSING_AT_RUNTIME", codes)
        self.assertNotIn("TASK_EVIDENCE_MISSING", codes)
        self.assertEqual(outcome.status, RunStatus.PASSED, codes)
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            [item["status"] for item in outcome.task_evidence], [EVIDENCE_SATISFIED]
        )

    def test_a_missing_mandatory_live_context_is_recorded_in_the_manifests_observed_runtime(self):
        """Negative case: the selected native task left no live context.

        The absence is written into the build manifest while the run is still
        online, so a later reader does not have to take the reader's word for it.
        """
        run_dir = self.build_package(write_context=False)

        recorded = json.loads(
            (run_dir / "build-manifest.json").read_text(encoding="utf-8")
        )["observed_runtime"]

        self.assertEqual(
            [entry["task_id"] for entry in recorded["missing_evidence"]], ["lock-exact-e"]
        )
        self.assertEqual(
            recorded["missing_evidence"][0]["expected_path"],
            str(
                Path(SUITE_RUNS_DIRNAME)
                / make_task_run_id(SUITE_ID, 1, "lock-exact-e")
                / "evidence"
                / make_task_run_id(SUITE_ID, 1, "lock-exact-e")
                / LIVE_CONTEXT_FILENAME
            ),
        )
        self.assertEqual(
            recorded["missing_evidence"][0]["reason"], "the task recorded no live-context.json"
        )
        self.assertEqual(recorded["live_contexts"], [])
        # The policy this run was graded against is recorded next to it.
        self.assertEqual(
            [item["task_id"] for item in recorded["policy"]], ["lock-exact-e"]
        )
        self.assertTrue(recorded["policy"][0]["requires_live_context"])

    def test_the_missing_context_surfaces_as_the_runtime_finding_and_blocks_passed(self):
        """A green suite must not be enough to grade the whole run PASSED."""
        run_dir = self.build_package(write_context=False)

        package = LoadedRunPackage.load(run_dir)
        outcome = evaluate_run(package)

        codes = [finding.code for finding in outcome.findings]
        self.assertIn("TASK_EVIDENCE_MISSING_AT_RUNTIME", codes)
        # The reader's own lookup agrees with what the run recorded.
        self.assertIn("TASK_EVIDENCE_MISSING", codes)
        self.assertEqual(outcome.suite_status, ExecutionStatus.FAILED.value)
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertEqual(outcome.exit_code, 1)
        runtime_finding = next(
            finding
            for finding in outcome.findings
            if finding.code == "TASK_EVIDENCE_MISSING_AT_RUNTIME"
        )
        self.assertEqual(runtime_finding.details["task_id"], "lock-exact-e")
        self.assertEqual(
            runtime_finding.details["run_id"], make_task_run_id(SUITE_ID, 1, "lock-exact-e")
        )


# ---------------------------------------------------------------------------
# 6. the collector really collects what the Go boundary producer writes
# ---------------------------------------------------------------------------
GO_TEST_JSON = (
    '{"Time":"2026-01-01T00:00:01Z","Action":"run",'
    '"Package":"github.com/gonka/forward-e2e/harness/go_boundary/query_faults",'
    '"Test":"TestToQuerierResultClassifiesVMSystemErrors"}\n'
    '{"Time":"2026-01-01T00:00:02Z","Action":"pass",'
    '"Package":"github.com/gonka/forward-e2e/harness/go_boundary/query_faults",'
    '"Test":"TestToQuerierResultClassifiesVMSystemErrors","Elapsed":0.31}\n'
    '{"Time":"2026-01-01T00:00:03Z","Action":"run",'
    '"Package":"github.com/gonka/forward-e2e/harness/go_boundary/query_faults",'
    '"Test":"TestStrictPlanValidation"}\n'
    '{"Time":"2026-01-01T00:00:04Z","Action":"pass",'
    '"Package":"github.com/gonka/forward-e2e/harness/go_boundary/query_faults",'
    '"Test":"TestStrictPlanValidation","Elapsed":0.12}\n'
    '{"Time":"2026-01-01T00:00:04Z","Action":"pass",'
    '"Package":"github.com/gonka/forward-e2e/harness/go_boundary/query_faults","Elapsed":0.51}\n'
)

BUILD_LOG = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#2 [builder 1/6] FROM docker.io/library/golang:1.24.2-alpine3.21\n"
    "#3 [builder 6/6] RUN mkdir -p /boundary-evidence; go test ...\n"
    "#4 exporting to client directory\n"
    "#4 DONE 0.4s\n"
)

DOCKERFILE_SHA256 = hashlib.sha256(b"synthetic-pinned-builder-dockerfile").hexdigest()


class GoBoundaryArtifactsAreCollectedTests(unittest.TestCase):
    """Exercises ``forward_e2e/suite/collector.py``: ``ALLOWED_ARTIFACT_PATTERNS``,
    ``collect_snapshot_artifacts``, ``update_artifact_index``; and
    ``forward_e2e/suite/verifier.py``: ``verify_suite_artifacts_integrity``.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name)
        self.runtime_root = self.root / "a8-runtime"
        self.suite_dir = self.root / "suite" / SUITE_ID
        self.suite_dir.mkdir(parents=True)
        self.catalog_task = get_task_by_id_or_alias("go-boundary")
        self.run_id = make_task_run_id(SUITE_ID, self.catalog_task.ordinal, self.catalog_task.task_id)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def write_producer_tree(self) -> RuntimeSnapshot:
        """Reproduce the directory ``scripts/run_go_boundary.py`` leaves behind.

        The adapter passes ``snapshot.task_evidence_dir`` (``evidence/<run-id>``)
        as the script's output directory; the script writes ``build.log`` and
        ``report.json`` there and lets ``docker build --output type=local`` export
        the buildx stage into ``raw/``. ``identity.json`` at the snapshot root is
        written by ``forward_e2e/suite/runtime.py``.
        """
        snapshot = RuntimeSnapshot(self.runtime_root, self.run_id)
        snapshot.run_dir.mkdir(parents=True)
        snapshot.evidence_dir.mkdir(parents=True, exist_ok=True)
        task_ev = snapshot.task_evidence_dir
        raw = task_ev / "raw"
        raw.mkdir(parents=True)

        (task_ev / "build.log").write_text(BUILD_LOG, encoding="utf-8")
        (raw / "go-test.json").write_text(GO_TEST_JSON, encoding="utf-8")
        (raw / "exit-code").write_text("0\n", encoding="utf-8")

        report = {
            "gonka_sha": REQUESTED_SHA,
            "level": "Go classification and JSON roundtrip, not FFI",
            "docker_exit": 0,
            "go_exit": "0",
            "dockerfile_sha256": DOCKERFILE_SHA256,
            "status": "PASS",
        }
        report["test_output_sha256"] = hashlib.sha256(
            (raw / "go-test.json").read_bytes()
        ).hexdigest()
        (task_ev / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )

        identity = {
            "run_id": self.run_id,
            "created_at_utc": "2026-01-01T00:00:00+00:00",
            "marketplace_source_sha": MARKETPLACE_SHA,
            "marketplace_commit_sha": MARKETPLACE_SHA,
            "marketplace_tree_sha": MARKETPLACE_TREE_SHA,
            "gonka_source_sha": REQUESTED_SHA,
            "gonka_commit_sha": REQUESTED_SHA,
            "gonka_tree_sha": GONKA_TREE_SHA,
            "source_immutability_verdict": "UNCHANGED",
            "evidence_model": EVIDENCE_MODEL_IMMUTABLE,
            "marketplace_bundle_sha256": "a" * 64,
            "gonka_bundle_sha256": "b" * 64,
            "run_dir": str(snapshot.run_dir),
        }
        snapshot.identity_file.write_text(
            json.dumps(identity, indent=2) + "\n", encoding="utf-8"
        )
        return snapshot

    def collect(self, snapshot: RuntimeSnapshot):
        exported, errors = collect_snapshot_artifacts(
            snapshot, self.catalog_task.task_id, self.suite_dir
        )
        update_artifact_index(self.suite_dir, exported)
        return exported, errors

    def go_boundary_plan(self) -> SuitePlan:
        return SuitePlan(
            schema_version="1.0.0",
            suite_id=SUITE_ID,
            created_at_utc="2026-01-01T00:00:00+00:00",
            profile=None,
            requested_scenarios=[self.catalog_task.task_id],
            source_identity=suite_source_identity(),
            tasks=[TaskPlan.from_dict(self.catalog_task.to_dict())],
        )

    def test_every_declared_go_boundary_artifact_reaches_the_export_and_the_index(self):
        """The two ``raw/`` files are the ones the allowlist used to drop."""
        snapshot = self.write_producer_tree()

        exported, errors = self.collect(snapshot)

        self.assertEqual(errors, [])
        exported_paths = {entry.relative_path for entry in exported}
        expected_paths = {
            f"runs/{self.run_id}/evidence/{self.run_id}/{declared}"
            for declared in self.catalog_task.expected_artifacts
        }
        self.assertEqual(
            sorted(self.catalog_task.expected_artifacts),
            ["build.log", "raw/exit-code", "raw/go-test.json", "report.json"],
        )
        self.assertTrue(expected_paths <= exported_paths, sorted(exported_paths))
        for relative in sorted(expected_paths):
            with self.subTest(artifact=relative):
                copied = self.suite_dir / relative
                self.assertTrue(copied.is_file(), relative)
        # The machine-readable Go output survives the copy byte for byte.
        self.assertEqual(
            (self.suite_dir / f"runs/{self.run_id}/evidence/{self.run_id}/raw/go-test.json")
            .read_bytes(),
            (snapshot.task_evidence_dir / "raw" / "go-test.json").read_bytes(),
        )
        index = json.loads(
            (self.suite_dir / "artifact-index.json").read_text(encoding="utf-8")
        )
        indexed = {item["relative_path"] for item in index["artifacts"]}
        self.assertTrue(expected_paths <= indexed, sorted(indexed))
        by_path = {item["relative_path"]: item for item in index["artifacts"]}
        for relative in sorted(expected_paths):
            with self.subTest(indexed=relative):
                self.assertEqual(by_path[relative]["task_id"], self.catalog_task.task_id)
                self.assertEqual(by_path[relative]["run_id"], self.run_id)
                self.assertEqual(
                    by_path[relative]["sha256"],
                    hashlib.sha256((self.suite_dir / relative).read_bytes()).hexdigest(),
                )

    def test_the_offline_reporter_finds_no_missing_mandatory_artifact_for_a_complete_tree(self):
        snapshot = self.write_producer_tree()
        _, errors = self.collect(snapshot)
        self.assertEqual(errors, [])

        _, _, problems = verify_suite_artifacts_integrity(
            self.suite_dir, plan=self.go_boundary_plan()
        )

        self.assertEqual(problems, [])

    def test_a_deleted_raw_go_test_json_is_reported_as_a_missing_mandatory_artifact(self):
        """Negative case: exactly one declared artefact is absent at the source.

        Everything else -- the layout, the other three artefacts, the identity
        and the index -- is what the real producer leaves behind.
        """
        snapshot = self.write_producer_tree()
        (snapshot.task_evidence_dir / "raw" / "go-test.json").unlink()

        exported, errors = self.collect(snapshot)

        self.assertEqual(errors, [])
        self.assertNotIn(
            f"runs/{self.run_id}/evidence/{self.run_id}/raw/go-test.json",
            {entry.relative_path for entry in exported},
        )
        # The other mandatory raw artefact still arrives, so this is not a
        # collector-wide failure being mistaken for a missing file.
        self.assertIn(
            f"runs/{self.run_id}/evidence/{self.run_id}/raw/exit-code",
            {entry.relative_path for entry in exported},
        )

        _, _, problems = verify_suite_artifacts_integrity(
            self.suite_dir, plan=self.go_boundary_plan()
        )

        self.assertIn(
            f"Task {self.catalog_task.task_id} run {self.run_id} missing mandatory artifact on disk: "
            "raw/go-test.json",
            problems,
        )
        self.assertEqual(
            [item for item in problems if "raw/exit-code" in item], [], problems
        )


if __name__ == "__main__":
    unittest.main()
