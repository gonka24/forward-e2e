"""Build and runtime provenance tests: selected vs observed Gonka commit,
source-tree immutability, the runner-owned harness binding, and A9 production
Wasm provenance.

Separate claims are proven here, because conflating any two of them is how an
unproven runtime used to pass:

* the commit the user *selected*, recorded at ``source.gonka_source_sha`` and
  ``source.gonka_sha`` alongside ``source.gonka_tree_sha``;
* the commit the running binary *observes*, recorded at
  ``source.runtime.gonka_source_sha``, which must equal the selected commit;
* the companion ``source-immutability.json`` record proving neither checkout
  drifted before or after the run.

The E2E runner requires the immutable-source evidence model and rejects
historical prepared-build and legacy model declarations. Deployment comparison
is shared by the online executor and the offline package reader; both must
preserve the same provenance verdicts.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from forward_e2e.suite.catalog import resolve_e2e_selection
from forward_e2e.execution.compat import (
    marketplace_contracts_adapter,
)
from forward_e2e.execution.context import E2ERunContext
from forward_e2e.execution.deployment import (
    INCOMPLETE,
    MATCHED,
    MISMATCHED,
    NOT_REPORTED,
    verify_deployed_artifacts,
)
from forward_e2e.execution.errors import BuildProvenanceError
from forward_e2e.execution.executor import (
    A9_MANIFEST_ROLE,
    A9_RELEASE_ROLE_PREFIX,
    RestoredSource,
    _build_context,
    _evidence_requirements,
    _verify_post_run_provenance,
)
from forward_e2e.execution.outcome import RunStatus, evaluate_run
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    EXECUTION_MANIFEST_SCHEMA,
    RUN_LOCK_FILENAME,
    BuildManifest,
    DeliveryAttempt,
    DeliveryManifest,
    DeliveryStatus,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    content_sha256,
    write_run_lock,
    write_delivery_manifest,
)
from forward_e2e.execution.runpackage import LoadedRunPackage, evidence_policy_verify_deployment
from forward_e2e.execution.sources import bundle_ref
from forward_e2e.suite.evidence_model import (
    EVIDENCE_MODEL_E2E,
    EVIDENCE_MODEL_FIELD,
    EVIDENCE_MODEL_IMMUTABLE,
    EVIDENCE_MODEL_LEGACY,
)
from forward_e2e.suite.collector import classify_artifact_kind
from forward_e2e.suite.models import (
    AcceptanceStatus, ArtifactEntry, CleanupStatus, EvidenceStatus, ExecutionStatus,
    ProofLevel, SourceIdentity, SuitePlan, SuiteResult, TaskResult, make_task_run_id,
)
from forward_e2e.suite.orchestrator import SuiteOrchestrator
from tests.unit.runner.real_fixtures import real_lock_exact_e_context
from tests.unit.runner.support.fakes import (
    GONKA_TREE_SHA,
    IMMUTABLE_EVIDENCE_FILES,
    MARKETPLACE_TREE_SHA,
    StubLayout,
    live_context_payload as fake_live_context_payload,
    make_test_adapter,
    ownership_payload,
    source_immutability_bytes,
)
from tests.unit.runner.support.packages import make_a9_build_manifest, make_baseline_lock
from forward_e2e.suite.verifier import verify_live_context

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The commit the operator asked to prove.
REQUESTED_SHA = "a" * 40
#: A distinct commit used for negative variants (e.g. a runtime reporting a
#: different binary or a stale prepared commit).
PREPARED_SHA = "b" * 40
#: A third commit used for single-fact negative variants.
FOREIGN_SHA = "c" * 40
MARKETPLACE_SHA = "d" * 40

#: The one task every lock fixture selects. It is a real catalog task, so the
#: evidence policy derives its real proof level (``NATIVE``) rather than one
#: asserted here.
SELECTED_TASK_ID = "lock-exact-e"
#: Suite id used wherever evidence has to be written at its real per-task path.
DEPLOYMENT_SUITE_ID = "suite-r10-deployment"

RUNNER_IMAGE_ID = "sha256:" + "1" * 64
DEAL_SHA256 = "2" * 64
FACTORY_SHA256 = "3" * 64
CALLER_SHA256 = "4" * 64
CW20_SHA256 = "5" * 64
A9_MANIFEST_SHA256 = "6" * 64

DEAL_ROLE = A9_RELEASE_ROLE_PREFIX + "marketplace_deal.wasm"
FACTORY_ROLE = A9_RELEASE_ROLE_PREFIX + "marketplace_factory.wasm"
CALLER_ROLE = "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm"
CW20_ROLE = "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm"


# ---------------------------------------------------------------------------
# synthetic fixtures with the real recorded shapes
# ---------------------------------------------------------------------------
_UNSET = object()


def live_context_payload(
    *,
    requested_sha: str = REQUESTED_SHA,
    prepared_sha: str = PREPARED_SHA,
    observed_sha: str | None | object = _UNSET,
    harness_sha: str | None | object = _UNSET,
    with_runtime: bool = True,
    evidence_model: str | None = EVIDENCE_MODEL_IMMUTABLE,
) -> dict:
    """A live-context.json with the shape scripts/acceptance_harness.py writes."""
    return fake_live_context_payload(
        requested_sha=requested_sha,
        prepared_sha=prepared_sha,
        observed_sha=requested_sha if observed_sha is _UNSET else observed_sha,
        harness_sha=harness_sha,
        marketplace_sha=MARKETPLACE_SHA,
        with_runtime=with_runtime,
        evidence_model=evidence_model,
    )


def expected_identity(*, requested_sha: str = REQUESTED_SHA) -> SourceIdentity:
    """What the orchestrator hands the verifier for an E2E run."""
    return SourceIdentity(
        marketplace_commit_sha=MARKETPLACE_SHA,
        gonka_commit_sha=requested_sha,
        runner_version_hash="7" * 64,
        catalog_version_hash="8" * 64,
        gonka_tree_sha=GONKA_TREE_SHA,
        marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        source_immutability_verdict="UNCHANGED",
    )


def make_lock() -> RunLock:
    """A complete run lock; only its identity and selection are read here."""
    return make_baseline_lock(
        scenarios=[SELECTED_TASK_ID],
        plan_id="plan-r10",
        gonka_sha=REQUESTED_SHA,
        contracts_sha=MARKETPLACE_SHA,
        gonka_bundle_sha256="9" * 64,
        contracts_bundle_sha256="e" * 64,
        gonka_bundle_ref=bundle_ref("gonka"),
        contracts_bundle_ref=bundle_ref("contracts"),
        limits={},
    )


make_adapter = make_test_adapter
make_manifest = make_a9_build_manifest


def write_companion_files(evidence_dir: Path) -> list[Path]:
    """Write source-immutability.json and companion files next to live-context.json."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    immut_path = evidence_dir / "source-immutability.json"
    immut_path.write_bytes(
        source_immutability_bytes(
            gonka_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        )
    )
    written = [immut_path]
    for rel_path, content in IMMUTABLE_EVIDENCE_FILES.items():
        target = evidence_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(target)
    return written


def write_producer_live_context(suite_dir: Path, run_id: str, payload: dict) -> Path:
    """Mirror the harness and collector layout independently of evidence lookup.

    The harness appends the run ID to its evidence directory; the collector
    preserves that path relative to the runtime snapshot.
    """
    path = suite_dir / "runs" / run_id / "evidence" / run_id / "live-context.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_companion_files(path.parent)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build_deployment_context(
    root: Path, manifest: BuildManifest, *, lock: RunLock, with_runtime: bool = False
) -> E2ERunContext:
    """Keep deployment fixtures on the same real executor preparation path."""
    return _build_context(
        lock=lock,
        layout=StubLayout(root / "runner"),
        gonka_adapter=make_adapter("gonka", with_runtime=with_runtime),
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


# ---------------------------------------------------------------------------
# Finding A: selected vs observed runtime commit and immutable-source fields
# ---------------------------------------------------------------------------
class RequestedPreparedObservedShaTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10prov-")
        self.root = Path(self.tmp_dir.name)
        self.context_path = self.root / "live-context.json"
        write_companion_files(self.root)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def write(self, payload) -> Path:
        self.context_path.write_text(json.dumps(payload), encoding="utf-8")
        return self.context_path

    def test_a_prepared_runtime_that_differs_from_the_requested_commit_is_accepted(self):
        """Under the immutable-source model, only the exact selected commit is accepted."""
        payload = live_context_payload()
        self.assertEqual(
            payload["source"]["gonka_source_sha"],
            payload["source"]["runtime"]["gonka_source_sha"],
        )

        ok, observed, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertIsNone(error)
        self.assertTrue(ok)
        # The recorded bootstrap section is the only checkpoint this fixture
        # can honestly claim.
        self.assertEqual(observed, ["bootstrap"])

    def test_a_runtime_reporting_the_requested_commit_instead_of_the_prepared_one_fails(self):
        """Single changed fact: the binary reports a commit other than the selected one."""
        payload = live_context_payload(observed_sha=PREPARED_SHA)

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)
        self.assertIn(repr(REQUESTED_SHA), error)
        self.assertIn(repr(PREPARED_SHA), error)

    def test_a_runtime_section_without_an_observed_commit_fails(self):
        """Single changed fact: ``source.runtime.gonka_source_sha`` is absent."""
        payload = live_context_payload(observed_sha=None)
        self.assertNotIn("gonka_source_sha", payload["source"]["runtime"])

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn(
            "live-context.json 'source.runtime' missing mandatory gonka_source_sha", error
        )
        self.assertIn(repr(REQUESTED_SHA), error)

    def test_a_context_without_any_runtime_identity_fails(self):
        """Single changed fact: the whole ``source.runtime`` section is gone."""
        payload = live_context_payload()
        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )
        self.assertIsNone(error)
        self.assertTrue(ok)

        del payload["source"]["runtime"]
        self.assertNotIn("runtime", payload["source"])

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("live-context.json 'source' missing mandatory 'runtime' identity", error)
        self.assertIn(repr(REQUESTED_SHA), error)

    def test_an_empty_runtime_identity_is_not_accepted_as_a_report(self):
        payload = live_context_payload()
        payload["source"]["runtime"] = {}

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("live-context.json 'source' missing mandatory 'runtime' identity", error)

    def test_invalid_runtime_documents_cannot_bypass_checks_by_declaring_the_legacy_model(self):
        """Changing the declaration cannot opt into weaker runtime checks."""
        variants = {
            "the binary reports a different commit": dict(observed_sha=PREPARED_SHA),
            "the runtime section names no commit": dict(observed_sha=None),
            "there is no runtime section": dict(with_runtime=False),
            "the runtime section is empty": dict(observed_sha=PREPARED_SHA),
        }
        for label, kwargs in variants.items():
            with self.subTest(variant=label):
                strict = live_context_payload(**kwargs)
                if label == "the runtime section is empty":
                    strict["source"]["runtime"] = {}

                ok, _, error = verify_live_context(
                    self.write(strict), expected_source_identity=expected_identity()
                )
                self.assertFalse(ok, msg=f"{label} must fail under the E2E model")

                relaxed = json.loads(json.dumps(strict))
                relaxed["source"][EVIDENCE_MODEL_FIELD] = EVIDENCE_MODEL_LEGACY
                # Exactly one fact differs between the two documents.
                self.assertEqual(
                    {
                        k: v
                        for k, v in strict["source"].items()
                        if k != EVIDENCE_MODEL_FIELD
                    },
                    {
                        k: v
                        for k, v in relaxed["source"].items()
                        if k != EVIDENCE_MODEL_FIELD
                    },
                )

                ok, _, error = verify_live_context(
                    self.write(relaxed), expected_source_identity=expected_identity()
                )

                self.assertFalse(ok)
                self.assertIn("weaker prepared-build or legacy rules", error)

    def test_a_top_level_source_sha_that_is_not_the_requested_commit_fails(self):
        """A correct runtime and fallback SHA must not hide a wrong selection.

        Only the top-level requested SHA changes; the nested runtime still
        reports the selected commit and gonka_sha still names the selected one.
        """
        payload = live_context_payload()
        payload["source"]["gonka_source_sha"] = FOREIGN_SHA

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("Gonka source commit SHA mismatch in live-context.json", error)
        self.assertIn(repr(REQUESTED_SHA), error)
        self.assertIn(repr(FOREIGN_SHA), error)

    def test_a_missing_prepared_test_harness_sha_is_not_compensated_by_the_source_sha(self):
        """A missing mandatory gonka_tree_sha is rejected by the immutable verifier."""
        payload = live_context_payload()
        del payload["source"]["gonka_tree_sha"]

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("gonka_tree_sha", error)

    def test_a_test_harness_sha_that_is_not_the_prepared_commit_fails(self):
        """Any retired prepared-build field in live-context.json source is rejected."""
        payload = live_context_payload()
        payload["source"]["gonka_test_harness_sha"] = FOREIGN_SHA

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("gonka_test_harness_sha", error)

    def test_the_marketplace_commit_is_still_checked_alongside_the_gonka_commits(self):
        payload = live_context_payload()
        payload["source"]["marketplace_commit_sha"] = FOREIGN_SHA

        ok, _, error = verify_live_context(
            self.write(payload), expected_source_identity=expected_identity()
        )

        self.assertFalse(ok)
        self.assertIn("Marketplace commit SHA mismatch in live-context.json", error)


# ---------------------------------------------------------------------------
# Finding A: producer/consumer coupling through the orchestrator
# ---------------------------------------------------------------------------
class OrchestratorSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10prov-")
        self.root = Path(self.tmp_dir.name)
        self.marketplace = self.root / "marketplace"
        self.gonka = self.root / "gonka"
        self.output_dir = self.root / "output"
        self.runtime_root = self.root / "runtime"
        for directory in (self.marketplace, self.gonka, self.output_dir, self.runtime_root):
            directory.mkdir(parents=True)

        self.e2e_context = E2ERunContext(
            harness_script=self.root / "runner" / "scripts" / "acceptance_harness.py",
            gonka_requested_sha=REQUESTED_SHA,
            contracts_requested_sha=MARKETPLACE_SHA,
            expected_gonka_sha=REQUESTED_SHA,
            expected_marketplace_sha=MARKETPLACE_SHA,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def run_failing_suite(self, *, suite_id: str, e2e_context) -> dict:
        """Drive the real ``run_suite`` far enough to write ``suite-plan.json``."""
        from tests.unit.runner.support.fakes import run_failing_suite_orchestrator

        _, plan = run_failing_suite_orchestrator(
            marketplace_dir=self.marketplace,
            gonka_dir=self.gonka,
            output_dir=self.output_dir,
            runtime_root=self.runtime_root,
            suite_id=suite_id,
            e2e_context=e2e_context,
            clean_heads=[MARKETPLACE_SHA, REQUESTED_SHA],
        )
        self.assertTrue(plan)
        return plan

    def test_the_suite_source_identity_carries_the_prepared_commit(self):
        """The suite source identity records the selected commits and tree SHAs."""
        plan = self.run_failing_suite(suite_id="suite-r10-prepared", e2e_context=self.e2e_context)

        identity = plan["source_identity"]
        self.assertEqual(identity["gonka_commit_sha"], REQUESTED_SHA)
        self.assertIsNone(identity.get("gonka_prepared_sha"))
        self.assertEqual(identity["gonka_tree_sha"], GONKA_TREE_SHA)
        # Deliberately supply a foreign checkout HEAD to prove that the
        # explicit E2E context takes precedence over checkout discovery.
        self.assertNotEqual(identity["gonka_commit_sha"], FOREIGN_SHA)
        self.assertEqual(identity["marketplace_commit_sha"], MARKETPLACE_SHA)

    def test_an_orchestrator_without_an_e2e_context_is_rejected(self):
        """The orchestrator requires an explicit target e2e_context."""
        with self.assertRaises(ValueError):
            SuiteOrchestrator(
                marketplace_dir=self.marketplace,
                gonka_dir=self.gonka,
                output_dir=self.output_dir,
                runtime_root=self.runtime_root,
                e2e_context=None,
            )

    def test_the_recorded_identity_activates_the_verifier_prepared_runtime_check(self):
        """The recorded identity supplies the verifier's expected selected SHA.

        Suite evidence evaluation is mocked by the orchestrator helper. Here
        we pass its recorded identity to the real verifier with an explicit
        E2E flag. Flag forwarding is tested in test_e2e_evidence_model.py.
        """
        plan = self.run_failing_suite(suite_id="suite-r10-coupled", e2e_context=self.e2e_context)
        identity = SourceIdentity.from_dict(plan["source_identity"])

        write_companion_files(self.root)
        context_path = self.root / "live-context.json"
        context_path.write_text(
            json.dumps(live_context_payload(observed_sha=PREPARED_SHA)), encoding="utf-8"
        )

        ok, _, error = verify_live_context(
            context_path,
            expected_source_identity=identity,
            e2e_evidence_expected=True,
        )

        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)

        context_path.write_text(json.dumps(live_context_payload()), encoding="utf-8")
        ok, _, error = verify_live_context(
            context_path,
            expected_source_identity=identity,
            e2e_evidence_expected=True,
        )
        self.assertIsNone(error)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Finding B: the harness that judges is the runner's own
# ---------------------------------------------------------------------------
class HarnessBindingTests(unittest.TestCase):
    """The Kotlin consumer of ``A8_HARNESS``.

    The producer side (``run-live`` exporting the runner's own script) is
    covered in ``tests/unit/harness/test_acceptance_harness.py``; this reads the
    consumer so that a rename on either side is caught.
    """

    def test_the_kotlin_test_runs_the_harness_named_by_a8_harness(self):
        kotlin = (
            REPO_ROOT
            / "harness"
            / "testermint"
            / "src"
            / "test"
            / "kotlin"
            / "MarketplaceContractAcceptanceTests.kt"
        )
        self.assertTrue(kotlin.is_file(), f"missing consumer source: {kotlin}")
        body = kotlin.read_text(encoding="utf-8")

        start = body.index("private fun runHarness(")
        snippet = body[start : body.index("private fun ", start + len("private fun "))]

        # The script that is executed comes from E2E_HARNESS (with A8_HARNESS alias)...
        self.assertIn('requiredEnv("E2E_PYTHON")', snippet)
        self.assertIn('requiredEnv("E2E_HARNESS")', snippet)
        # ...while the target checkout is only the working directory.
        self.assertIn('File(requiredEnv("E2E_MARKETPLACE_DIR"))', snippet)
        self.assertNotIn('E2E_MARKETPLACE_DIR") + "/scripts', snippet)
        self.assertNotIn('A8_MARKETPLACE_DIR") + "/scripts', snippet)
        self.assertNotIn("a8_acceptance.py", snippet)
        self.assertNotIn("acceptance_harness.py", snippet)
        self.assertIn("requiredHarnessEnv(canonicalName, legacyName)", body)


# ---------------------------------------------------------------------------
# Finding C: A9 production Wasm provenance
# ---------------------------------------------------------------------------
class ContractReleaseRecipeTests(unittest.TestCase):
    def test_the_a9_release_step_declares_both_production_wasm_artifacts(self):
        """Declaring them is what makes the builder hash the real binaries."""
        adapter = marketplace_contracts_adapter()
        self.assertIsNotNone(adapter.build)
        self.assertEqual(adapter.build.recipe_id, "contracts-a9-release-v1")

        steps = {step.step_id: step for step in adapter.build.steps}
        self.assertIn("a9-release", steps)
        produced = tuple(steps["a9-release"].produces_files)

        self.assertIn("a9-release/build-manifest.json", produced)
        self.assertIn("a9-release/wasm/marketplace_deal.wasm", produced)
        self.assertIn("a9-release/wasm/marketplace_factory.wasm", produced)
        self.assertEqual(A9_MANIFEST_ROLE, "a9-release/build-manifest.json")

    def test_the_harness_fixtures_stay_in_their_own_step(self):
        """Fixtures are built too, but never under the release role prefix."""
        adapter = marketplace_contracts_adapter()
        steps = {step.step_id: step for step in adapter.build.steps}
        fixtures = tuple(steps["test-contracts"].produces_files)

        self.assertIn(CALLER_ROLE, fixtures)
        self.assertIn(CW20_ROLE, fixtures)
        for role in fixtures:
            self.assertFalse(role.startswith(A9_RELEASE_ROLE_PREFIX))


class BuildContextDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10prov-")
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def build_context(self, manifest: BuildManifest) -> E2ERunContext:
        return build_deployment_context(self.root, manifest, lock=make_lock(), with_runtime=True)

    def test_only_the_production_wasm_are_recorded_as_deployed(self):
        manifest = make_manifest()

        self.build_context(manifest)

        deployed = manifest.deployment["production_wasm"]
        self.assertEqual(
            [item["role"] for item in deployed], [DEAL_ROLE, FACTORY_ROLE]
        )
        self.assertEqual(
            [item["sha256"] for item in deployed], [DEAL_SHA256, FACTORY_SHA256]
        )

    def test_the_harness_fixtures_are_accounted_for_separately(self):
        """A fixture hash must never be able to stand in for a production one."""
        manifest = make_manifest()

        self.build_context(manifest)

        fixtures = manifest.deployment["fixture_wasm"]
        self.assertEqual([item["role"] for item in fixtures], [CALLER_ROLE, CW20_ROLE])
        production_roles = {item["role"] for item in manifest.deployment["production_wasm"]}
        self.assertEqual(production_roles & {item["role"] for item in fixtures}, set())
        self.assertEqual(len(manifest.deployment["production_wasm"]) + len(fixtures), 4)

    def test_the_verified_release_manifest_is_handed_to_the_suite(self):
        manifest = make_manifest(build_out=str(self.root / "build"))

        context = self.build_context(manifest)

        expected_path = self.root / "build" / A9_MANIFEST_ROLE
        self.assertEqual(context.a9_manifest_path, expected_path)
        self.assertEqual(manifest.deployment["a9_manifest_path"], str(expected_path))
        self.assertEqual(manifest.deployment["a9_manifest_sha256"], A9_MANIFEST_SHA256)

    def test_the_context_keeps_the_requested_and_prepared_commits_apart(self):
        manifest = make_manifest()

        context = self.build_context(manifest)

        self.assertEqual(context.gonka_requested_sha, REQUESTED_SHA)
        self.assertEqual(context.contracts_requested_sha, MARKETPLACE_SHA)
        self.assertEqual(context.expected_gonka_sha, REQUESTED_SHA)
        self.assertEqual(context.expected_marketplace_sha, MARKETPLACE_SHA)
        self.assertFalse(hasattr(context, "gonka_prepared_sha"))


class DeployedArtifactProvenanceTests(unittest.TestCase):
    """The comparison between what was deployed and what was built.

    Round 10 ran this inside ``executor._verify_deployed_artifacts``, which
    raised on a mismatch. Round 11 replaced it with the shared, *reporting*
    ``forward_e2e/execution/deployment.verify_deployed_artifacts`` so that the online pass
    and the offline reader cannot drift apart -- which means the refusal itself
    now lives one level up, in the executor. Every negative below is therefore
    proven twice: once as the record both callers produce, and, where it is a
    mismatch, once as the run-stopping error the executor still raises from it.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10prov-")
        self.root = Path(self.tmp_dir.name)
        self.suite_dir = self.root / "suite" / DEPLOYMENT_SUITE_ID

    def tearDown(self):
        self.tmp_dir.cleanup()

    def prepared_deployment(self, manifest: BuildManifest) -> BuildManifest:
        """Fill ``manifest.deployment`` through the real executor code path."""
        build_deployment_context(self.root, manifest, lock=make_lock())
        return manifest

    def prepared_manifest(self, **kwargs) -> BuildManifest:
        """A manifest whose ``deployment`` was filled by the real code path."""
        return self.prepared_deployment(make_manifest(**kwargs))

    # -- the two callers of the one comparator -----------------------------
    def verify(self, payload, manifest: BuildManifest) -> dict:
        """Call the shared comparator exactly as the executor calls it.

        The same record is then re-derived through ``runpackage``'s offline
        entry point and required to be identical. That equality is the point of
        sharing the function: an offline report can no longer reach a different
        conclusion than the run it is reporting on.
        """
        report = verify_deployed_artifacts(
            payload,
            built_wasm=manifest.wasm,
            expected_manifest_sha256=(manifest.deployment or {}).get(
                "a9_manifest_sha256"
            ),
        )
        self.assertEqual(
            report,
            evidence_policy_verify_deployment(payload, build=manifest),
            msg="the online and offline deployment checks must not drift",
        )
        return report

    def refuses(self, payload, manifest: BuildManifest) -> BuildProvenanceError:
        """A mismatching record must still end the run, and does so here.

        The comparator reports; ``_verify_post_run_provenance`` is the place
        that turns ``MISMATCHED`` into a failure, so the negative is asserted
        where the decision is actually made.
        """
        requirements, _ = _evidence_requirements(
            lock=make_lock(),
            suite_id=DEPLOYMENT_SUITE_ID,
            suite_dir=self.suite_dir,
            manifest=manifest,
        )
        write_producer_live_context(self.suite_dir, requirements[0].run_id, payload)
        with self.assertRaises(BuildProvenanceError) as cm:
            _verify_post_run_provenance(
                self.suite_dir,
                manifest=manifest,
                adapter=make_adapter("gonka"),
                sources_root=self.root / "src" / "gonka",
                selected_sha=REQUESTED_SHA,
                marketplace_sha=MARKETPLACE_SHA,
                requirements=requirements,
                emit=lambda message: None,
            )
        self.assertIn(
            "The contracts deployed by the suite are not the ones this build produced",
            str(cm.exception),
        )
        return cm.exception

    # -- the positive ------------------------------------------------------
    def test_evidence_hashes_that_match_this_build_are_accepted(self):
        manifest = self.prepared_manifest()
        payload = live_context_payload()

        report = self.verify(payload, manifest)

        self.assertEqual(report["status"], MATCHED)
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["checked"]["a9_manifest_sha256"], A9_MANIFEST_SHA256)
        self.assertEqual(report["checked"][DEAL_ROLE], DEAL_SHA256)
        self.assertEqual(report["checked"][FACTORY_ROLE], FACTORY_SHA256)
        # The comparator no longer knows where the document came from; the
        # path is attached by the caller that read it (see
        # ``GreenRunDeploymentEvidenceTests``), so it is not asserted here.
        self.assertNotIn("path", report)

    def test_production_wasm_without_release_manifest_hash_is_incomplete(self):
        """Matching contract hashes cannot replace the missing release manifest."""
        manifest = make_manifest()
        manifest.binaries = [
            item for item in manifest.binaries
            if item.get("role") != A9_MANIFEST_ROLE
        ]
        self.prepared_deployment(manifest)

        report = self.verify(live_context_payload(), manifest)
        self.assertEqual(report["status"], INCOMPLETE)
        self.assertIn(A9_MANIFEST_ROLE, report["missing"])

    def test_invalid_or_ambiguous_build_hashes_cannot_match(self):
        """A hash must be a digest, and a role must identify one artifact."""
        cases = ("manifest", "deal", "duplicate_deal")
        for case in cases:
            with self.subTest(case=case):
                manifest = self.prepared_manifest()
                payload = live_context_payload()
                if case == "manifest":
                    manifest.deployment["a9_manifest_sha256"] = "not-a-sha256"
                    payload["source"]["a9_manifest_sha256"] = "not-a-sha256"
                    role = A9_MANIFEST_ROLE
                elif case == "deal":
                    item = next(item for item in manifest.wasm if item["role"] == DEAL_ROLE)
                    item["sha256"] = "not-a-sha256"
                    payload["source"]["a9_contract_sha256"]["deal"] = "not-a-sha256"
                    role = DEAL_ROLE
                else:
                    item = next(item for item in manifest.wasm if item["role"] == DEAL_ROLE)
                    manifest.wasm.append(dict(item))
                    role = DEAL_ROLE

                report = self.verify(payload, manifest)
                self.assertNotEqual(report["status"], MATCHED)
                self.assertIn(role, report["missing"])

    # -- mismatches: the deployed artefact is from another build -----------
    def test_a_deployed_artifact_hash_from_another_build_is_rejected(self):
        cases = [
            (
                "deal",
                lambda p: p["source"]["a9_contract_sha256"].__setitem__("deal", "9" * 64),
                DEAL_ROLE,
                DEAL_SHA256,
            ),
            (
                "factory",
                lambda p: p["source"]["a9_contract_sha256"].__setitem__("factory", "9" * 64),
                FACTORY_ROLE,
                FACTORY_SHA256,
            ),
            (
                "release_manifest",
                lambda p: p["source"].__setitem__("a9_manifest_sha256", "9" * 64),
                A9_MANIFEST_ROLE,
                A9_MANIFEST_SHA256,
            ),
        ]
        for name, mutate, role, built_sha in cases:
            with self.subTest(case=name):
                manifest = self.prepared_manifest()
                payload = live_context_payload()
                mutate(payload)

                report = self.verify(payload, manifest)
                self.assertEqual(report["status"], MISMATCHED)
                self.assertEqual(
                    report["mismatches"],
                    [{"artefact": role, "built": built_sha, "deployed": "9" * 64}],
                )

                error = self.refuses(payload, manifest)
                self.assertEqual(
                    [item["artefact"] for item in error.details["mismatches"]], [role]
                )

    def test_a_fixture_hash_can_never_satisfy_a_production_role(self):
        """Swapping in the harness CW20 hash is a mismatch, not a pass."""
        manifest = self.prepared_manifest()
        payload = live_context_payload()
        payload["source"]["a9_contract_sha256"]["deal"] = CW20_SHA256

        report = self.verify(payload, manifest)

        self.assertEqual(report["status"], MISMATCHED)
        self.assertEqual(report["mismatches"][0]["artefact"], DEAL_ROLE)
        self.assertEqual(report["mismatches"][0]["deployed"], CW20_SHA256)
        # The fixture really was built, under its own role, by this same build.
        self.assertIn(CW20_ROLE, {item["role"] for item in manifest.wasm})

        self.refuses(payload, manifest)

    # -- silence: a produced artefact that the evidence does not name -------
    def test_an_absent_deployed_artifact_hash_makes_the_finding_incomplete(self):
        """Silence is not agreement: every produced artefact must be named."""
        cases = [
            (
                "deal",
                lambda p: p["source"]["a9_contract_sha256"].pop("deal"),
                DEAL_ROLE,
                lambda r: (
                    self.assertNotIn(DEAL_ROLE, r["checked"]),
                    self.assertEqual(r["checked"][FACTORY_ROLE], FACTORY_SHA256),
                ),
            ),
            (
                "factory",
                lambda p: p["source"]["a9_contract_sha256"].pop("factory"),
                FACTORY_ROLE,
                lambda r: self.assertEqual(r["checked"][DEAL_ROLE], DEAL_SHA256),
            ),
            (
                "release_manifest",
                lambda p: p["source"].pop("a9_manifest_sha256"),
                A9_MANIFEST_ROLE,
                lambda r: self.assertNotIn("a9_manifest_sha256", r["checked"]),
            ),
        ]
        for name, mutate, missing_role, extra_checks in cases:
            with self.subTest(case=name):
                manifest = self.prepared_manifest()
                payload = live_context_payload()
                mutate(payload)

                report = self.verify(payload, manifest)
                self.assertEqual(report["status"], INCOMPLETE)
                self.assertEqual(report["missing"], [missing_role])
                extra_checks(report)

    def test_a_recorded_contract_without_a_valid_build_hash_cannot_match(self):
        for role in (DEAL_ROLE, FACTORY_ROLE):
            for value in (_UNSET, "", None, 123):
                with self.subTest(role=role, value=value):
                    manifest = self.prepared_manifest()
                    payload = live_context_payload()
                    artifact = next(item for item in manifest.wasm if item["role"] == role)
                    # Change only the mandatory build hash in a positive fixture.
                    if value is _UNSET:
                        del artifact["sha256"]
                    else:
                        artifact["sha256"] = value

                    report = self.verify(payload, manifest)

                    self.assertEqual(report["status"], INCOMPLETE)
                    self.assertEqual(report["missing"], [role])
                    self.assertEqual(report["mismatches"], [])
                    self.assertNotIn(role, report["checked"])
                    other_role = FACTORY_ROLE if role == DEAL_ROLE else DEAL_ROLE
                    self.assertIn(other_role, report["checked"])

    def test_an_emptied_hash_string_counts_as_absent_rather_than_as_a_report(self):
        for field, role in (("deal", DEAL_ROLE), ("factory", FACTORY_ROLE),
                            ("a9_manifest_sha256", A9_MANIFEST_ROLE)):
            with self.subTest(field=field):
                manifest = self.prepared_manifest()
                payload = live_context_payload()
                source = payload["source"]
                target = source if field == "a9_manifest_sha256" else source["a9_contract_sha256"]
                target[field] = ""

                report = self.verify(payload, manifest)

                self.assertEqual(report["status"], INCOMPLETE)
                self.assertEqual(report["missing"], [role])

    def test_an_absent_contract_hash_section_reports_both_production_roles_missing(self):
        manifest = self.prepared_manifest()
        payload = live_context_payload()
        del payload["source"]["a9_contract_sha256"]

        report = self.verify(payload, manifest)

        self.assertEqual(report["checked"], {"a9_manifest_sha256": A9_MANIFEST_SHA256})
        self.assertEqual(report["status"], INCOMPLETE)
        self.assertEqual(sorted(report["missing"]), sorted([DEAL_ROLE, FACTORY_ROLE]))

    def test_a_build_with_no_production_wasm_has_nothing_to_tie(self):
        """``NOT_REPORTED`` is reserved for a build that produced no contracts.

        Only the harness fixtures were built, so there is no production
        artefact the evidence could be asked about; that is genuinely different
        from a build that produced contracts and was answered with silence.
        """
        manifest = make_manifest()
        manifest.binaries = []
        manifest.wasm = [
            item
            for item in manifest.wasm
            if not str(item["role"]).startswith(A9_RELEASE_ROLE_PREFIX)
        ]
        self.prepared_deployment(manifest)
        self.assertEqual(manifest.deployment["production_wasm"], [])

        payload = live_context_payload()
        del payload["source"]["a9_contract_sha256"]
        del payload["source"]["a9_manifest_sha256"]

        report = self.verify(payload, manifest)

        self.assertEqual(report["checked"], {})
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["status"], NOT_REPORTED)

    def test_such_a_build_also_owes_its_task_no_deployment_evidence_at_all(self):
        """Round 11 / finding F1, at the same fact this class is about.

        Round 10 asked every live context for deployment hashes. The policy now
        derives the duty from the build: with no production Wasm there is
        nothing to state, so the native task's requirement is switched off
        rather than answered with ``NOT_REPORTED`` forever.
        """
        manifest = make_manifest()
        manifest.binaries = []
        manifest.wasm = []
        self.prepared_deployment(manifest)

        requirements, _ = _evidence_requirements(
            lock=make_lock(),
            suite_id=DEPLOYMENT_SUITE_ID,
            suite_dir=self.suite_dir,
            manifest=manifest,
        )

        self.assertEqual([req.task_id for req in requirements], [SELECTED_TASK_ID])
        # It is a native task, so it still owes the live context itself...
        self.assertTrue(requirements[0].requires_live_context)
        # ...but this build gave it no contracts to account for.
        self.assertFalse(requirements[0].requires_deployment_evidence)


class GreenRunDeploymentEvidenceTests(unittest.TestCase):
    """A passing suite has to say which contracts it deployed."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r10prov-")
        self.root = Path(self.tmp_dir.name)
        self.lock = make_lock()
        self.suite_id = "suite-r10-green"
        self.suite_dir = self.root / "suite" / self.suite_id
        self.suite_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- fixtures ---------------------------------------------------------
    def requirements(self, manifest: BuildManifest):
        """The duties of this run, derived the way the executor derives them.

        Round 10 let the post-run pass find any ``live-context.json`` under the
        suite by recursive glob. Round 11 replaced that with the selection's
        own requirements, so the fixture has to produce them the same way the
        production caller does rather than assert a path by hand.
        """
        requirements, disagreements = _evidence_requirements(
            lock=self.lock,
            suite_id=self.suite_id,
            suite_dir=self.suite_dir,
            manifest=manifest,
        )
        self.assertEqual(disagreements, [])
        return requirements

    def write_live_context(self, payload, requirement) -> Path:
        """Write evidence at the producer's path, independently of the reader."""
        return write_producer_live_context(self.suite_dir, requirement.run_id, payload)

    def prepared_manifest(self) -> BuildManifest:
        manifest = make_manifest(lock=self.lock)
        manifest.lock_sha256 = self.lock.lock_sha256
        manifest.plan_id = self.lock.plan_id
        manifest.run_id = self.suite_id
        build_deployment_context(self.root, manifest, lock=self.lock)
        return manifest

    def post_run(self, manifest: BuildManifest, requirements=None) -> dict:
        return _verify_post_run_provenance(
            self.suite_dir,
            manifest=manifest,
            adapter=make_adapter("gonka"),
            sources_root=self.root / "src" / "gonka",
            selected_sha=REQUESTED_SHA,
            marketplace_sha=MARKETPLACE_SHA,
            requirements=(
                self.requirements(manifest) if requirements is None else requirements
            ),
            emit=lambda message: None,
        )

    # -- the online pass ---------------------------------------------------
    def test_the_post_run_pass_marks_silent_evidence_as_incomplete(self):
        """This finding is the input the green-run rule refuses to accept."""
        manifest = self.prepared_manifest()
        requirements = self.requirements(manifest)
        payload = live_context_payload()
        del payload["source"]["a9_contract_sha256"]["deal"]
        self.write_live_context(payload, requirements[0])

        findings = self.post_run(manifest, requirements)

        self.assertEqual(len(findings["deployment"]), 1)
        finding = findings["deployment"][0]
        self.assertEqual(finding["status"], INCOMPLETE)
        self.assertEqual(finding["missing"], [DEAL_ROLE])
        self.assertEqual(finding["task_id"], SELECTED_TASK_ID)
        self.assertEqual(
            finding["path"],
            str(
                Path("runs") / requirements[0].run_id
                / "evidence" / requirements[0].run_id / "live-context.json"
            ),
        )

    def test_the_post_run_pass_marks_complete_evidence_as_matched(self):
        manifest = self.prepared_manifest()
        requirements = self.requirements(manifest)
        self.write_live_context(live_context_payload(), requirements[0])

        findings = self.post_run(manifest, requirements)

        finding = findings["deployment"][0]
        self.assertEqual(finding["status"], MATCHED)
        self.assertEqual(finding["missing"], [])

    def test_a_suite_that_left_no_live_context_produces_no_deployment_finding(self):
        """The empty-findings case the green-run rule also has to refuse.

        Round 11 added the second half of this claim: silence is no longer
        merely an absence of findings, it is recorded as a named missing duty,
        so a reader can tell "nothing was owed" from "what was owed is gone".
        """
        manifest = self.prepared_manifest()
        requirements = self.requirements(manifest)

        manifest.images.append({
            "role": "gonka/inference-chain:e2e", "reference": "gonka/inference-chain:e2e",
            "image_id": RUNNER_IMAGE_ID, "repo_digests": [],
            "created_epoch": 1.0, "step_id": "chain-images",
        })
        requirement = requirements[0]
        ownership = (
            self.suite_dir / "runs" / requirement.run_id
            / "cleanup-evidence" / "ownership.json"
        )
        ownership.parent.mkdir(parents=True, exist_ok=True)
        ownership.write_text(json.dumps(ownership_payload(
            run_id=requirement.run_id, image_id=RUNNER_IMAGE_ID,
        )), encoding="utf-8")
        context_file = self.write_live_context(live_context_payload(), requirement)
        positive = self.post_run(manifest, requirements)
        self.assertEqual(positive["missing_evidence"], [])
        self.assertEqual(positive["deployment"][0]["status"], MATCHED)

        # Remove exactly one mandatory artifact from the complete fixture.
        context_file.unlink()
        findings = self.post_run(manifest, requirements)

        self.assertEqual(findings["deployment"], [])
        self.assertTrue(manifest.deployment["production_wasm"])
        self.assertEqual(
            findings["missing_evidence"],
            [{
                "task_id": SELECTED_TASK_ID,
                "run_id": requirement.run_id,
                "expected_path": str(context_file.relative_to(self.suite_dir)),
                "reason": "the task recorded no live-context.json",
            }],
        )

    # The online refusal on a mismatch is proven per artefact in
    # ``DeployedArtifactProvenanceTests.refuses`` and is not repeated here.

    # -- the graded run package -------------------------------------------
    def write_run_package(self, *, payload, suite_status: str = "PASSED") -> Path:
        """Write a synthetic package for the real offline grader.

        Result documents use production model serializers. Identity, ownership,
        and harness artifacts are synthetic; business phases come from recorded
        evidence. Artifact kinds follow the collector's task-relative paths.
        """
        write_run_lock(self.lock, self.root / RUN_LOCK_FILENAME)

        manifest = self.prepared_manifest()
        manifest.images.append({
            "role": "gonka/inference-chain:e2e", "reference": "gonka/inference-chain:e2e",
            "image_id": RUNNER_IMAGE_ID, "repo_digests": [],
            "created_epoch": 1.0, "step_id": "chain-images",
        })
        manifest.finish()
        self.assertEqual(manifest.status, ManifestStatus.COMPLETE)
        manifest.write(self.root / BUILD_MANIFEST_FILENAME)

        task_run_id = make_task_run_id(self.suite_id, 1, SELECTED_TASK_ID)
        run_dir = self.suite_dir / "runs" / task_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        requirements = self.requirements(manifest)
        if payload is not None:
            payload = dict(payload)
            payload["run_id"] = task_run_id
            payload["scenarios"] = {
                "lock-exact-e": real_lock_exact_e_context()["scenarios"]["lock-exact-e"]
            }
            self.write_live_context(payload, requirements[0])
        ident_data = {
            "run_id": task_run_id,
            "task_id": SELECTED_TASK_ID,
            "marketplace_commit_sha": MARKETPLACE_SHA,
            "marketplace_tree_sha": MARKETPLACE_TREE_SHA,
            "gonka_commit_sha": REQUESTED_SHA,
            "gonka_source_sha": REQUESTED_SHA,
            "gonka_tree_sha": GONKA_TREE_SHA,
            "source_immutability_verdict": "UNCHANGED",
            "evidence_model": EVIDENCE_MODEL_IMMUTABLE,
        }
        (run_dir / "identity.json").write_text(
            json.dumps(ident_data, indent=2) + "\n", encoding="utf-8"
        )
        tasks, resolved_profile, resolved_scenarios = resolve_e2e_selection(
            profile=None, scenarios=[SELECTED_TASK_ID]
        )
        task_res = TaskResult(
            task_id=SELECTED_TASK_ID,
            ordinal=1,
            run_id=task_run_id,
            proof_level=ProofLevel.NATIVE,
            execution_status=ExecutionStatus(suite_status),
            evidence_status=EvidenceStatus.COMPLETE if payload is not None else EvidenceStatus.INCOMPLETE,
            cleanup_status=CleanupStatus.CLEANED,
            acceptance_status=AcceptanceStatus.NOT_REVIEWED,
            start_time_utc="2026-01-01T00:00:00+00:00",
            end_time_utc="2026-01-01T00:01:00+00:00",
            duration_seconds=60.0,
            phase="COMPLETED",
            exit_code=0 if suite_status == "PASSED" else 1,
            primary_failure=None if suite_status == "PASSED" else "Synthetic task failure",
            secondary_errors=[],
            expected_cases=list(tasks[0].expected_checkpoints),
            observed_passed_cases=list(tasks[0].expected_checkpoints) if suite_status == "PASSED" else [],
            missing_cases=[] if suite_status == "PASSED" else list(tasks[0].expected_checkpoints),
            missing_artifacts=[] if payload is not None else ["live-context.json"],
            raw_evidence_dir=str(self.root / "runtime" / task_run_id),
            exported_evidence_dir=str(run_dir),
        )
        (run_dir / "result.json").write_text(
            json.dumps(task_res.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

        plan = SuitePlan(
            schema_version="1.0.0",
            suite_id=self.suite_id,
            created_at_utc="2026-01-01T00:00:00+00:00",
            profile=resolved_profile,
            requested_scenarios=resolved_scenarios,
            source_identity=SourceIdentity(
                marketplace_commit_sha=MARKETPLACE_SHA,
                gonka_commit_sha=REQUESTED_SHA,
                gonka_tree_sha=GONKA_TREE_SHA,
                marketplace_tree_sha=MARKETPLACE_TREE_SHA,
                source_immutability_verdict="UNCHANGED",
                runner_version_hash="7" * 64,
                catalog_version_hash="8" * 64,
            ),
            tasks=tasks,
        )
        (self.suite_dir / "suite-plan.json").write_text(
            json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

        suite_result = SuiteResult(
            schema_version="1.0.0",
            suite_id=self.suite_id,
            created_at_utc=plan.created_at_utc,
            completed_at_utc="2026-01-01T00:01:00+00:00",
            source_identity=plan.source_identity,
            overall_status=ExecutionStatus(suite_status),
            tasks=[task_res],
            summary_message=f"Synthetic suite: {suite_status}",
        )
        (self.suite_dir / "suite-result.json").write_text(
            json.dumps(suite_result.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

        ownership = run_dir / "cleanup-evidence" / "ownership.json"
        ownership.parent.mkdir(parents=True, exist_ok=True)
        ownership.write_text(json.dumps(ownership_payload(run_id=task_run_id, image_id=RUNNER_IMAGE_ID)))
        for exp_art in (tasks[0].expected_artifacts or []):
            if exp_art == "live-context.json":
                continue
            art_file = run_dir / exp_art
            art_file.parent.mkdir(parents=True, exist_ok=True)
            if exp_art.endswith(".xml"):
                art_file.write_text(
                    "<testsuite name='MarketplaceContractAcceptanceTests' tests='1' failures='0' errors='0'>\n"
                    "  <testcase name='marketplace funded lock succeeds exactly at E' classname='MarketplaceContractAcceptanceTests'/>\n"
                    "</testsuite>\n",
                    encoding="utf-8",
                )
            elif exp_art.endswith(".wasm"):
                art_file.write_bytes(b"\x00asm\x01\x00\x00\x00")
            elif exp_art.endswith(".log"):
                art_file.write_text("testermint log content\n", encoding="utf-8")
            else:
                art_file.write_text(f"{exp_art} content\n", encoding="utf-8")

        entries = []
        for fpath in sorted(
            (p for p in run_dir.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(self.suite_dir).as_posix(),
        ):
            rel_p = fpath.relative_to(self.suite_dir).as_posix()
            entries.append(ArtifactEntry(
                relative_path=rel_p,
                size_bytes=fpath.stat().st_size,
                sha256=hashlib.sha256(fpath.read_bytes()).hexdigest(),
                run_id=task_run_id,
                task_id=SELECTED_TASK_ID,
                artifact_kind=classify_artifact_kind(fpath.relative_to(run_dir).as_posix()),
            ).to_dict())
        (self.suite_dir / "artifact-index.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "total_artifacts": len(entries),
                    "artifacts": entries,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        ExecutionManifest(
            schema_version=EXECUTION_MANIFEST_SCHEMA,
            lock_sha256=self.lock.lock_sha256,
            plan_id=self.lock.plan_id,
            run_id=self.suite_id,
            suite_id=self.suite_id,
            created_at_utc="2026-01-01T00:10:00+00:00",
            command="run",
            build_manifest_sha256=content_sha256(manifest.to_dict()),
            suite_export_relpath=f"suite/{self.suite_id}",
            source_immutability={
                "verdict": manifest.source_immutability["verdict"],
                "after_execution": {
                    role: dict(manifest.source_immutability["roles"][role]["after_execution"])
                    for role in ("gonka", "contracts")
                },
            },
        ).write(self.root / EXECUTION_MANIFEST_FILENAME)
        write_delivery_manifest(
            DeliveryManifest(
                run_id=self.suite_id,
                status=DeliveryStatus.COMPLETED,
                attempts=[DeliveryAttempt(
                    attempt_number=1,
                    command="run",
                    started_at_utc="2026-01-01T00:10:00+00:00",
                    completed_at_utc="2026-01-01T00:10:01+00:00",
                    destination=str(self.root),
                    status=DeliveryStatus.COMPLETED,
                    error=None,
                )],
            ),
            self.root / "delivery.json",
        )
        return self.root

    def grade(self, **kwargs):
        return evaluate_run(LoadedRunPackage.load(self.write_run_package(**kwargs)))

    def assert_missing_deployment_hash_prevents_a_pass(self, field, role):
        """Exercise the offline verdict with exactly one missing hash."""
        payload = live_context_payload()
        source = payload["source"]
        target = source if field == "a9_manifest_sha256" else source["a9_contract_sha256"]
        del target[field]

        outcome = self.grade(payload=payload)
        findings = {finding.code: finding for finding in outcome.findings}

        self.assertEqual(outcome.suite_status, "PASSED")
        self.assertNotEqual(outcome.status, RunStatus.PASSED)
        self.assertNotEqual(outcome.exit_code, 0)
        self.assertIn("DEPLOYED_ARTEFACT_NOT_STATED", findings)
        self.assertEqual(
            findings["DEPLOYED_ARTEFACT_NOT_STATED"].details["missing"], [role]
        )

    def test_a_green_run_without_the_deployed_deal_hash_is_not_passed(self):
        self.assert_missing_deployment_hash_prevents_a_pass("deal", DEAL_ROLE)

    def test_a_green_run_without_the_deployed_factory_hash_is_not_passed(self):
        self.assert_missing_deployment_hash_prevents_a_pass("factory", FACTORY_ROLE)

    def test_a_green_run_without_the_release_manifest_hash_is_not_passed(self):
        self.assert_missing_deployment_hash_prevents_a_pass("a9_manifest_sha256", A9_MANIFEST_ROLE)

    def test_the_same_run_that_states_the_hashes_it_deployed_is_passed(self):
        """The positive control: only the deployment statement differs."""
        outcome = self.grade(payload=live_context_payload())

        self.assertEqual([finding.code for finding in outcome.findings], [])
        self.assertEqual(outcome.status, RunStatus.PASSED)
        self.assertEqual(outcome.exit_code, 0)

    def test_a_green_run_whose_evidence_names_another_builds_contracts_is_not_passed(self):
        """Offline, a mismatch is a finding rather than a raised error."""
        payload = live_context_payload()
        payload["source"]["a9_contract_sha256"]["factory"] = "9" * 64

        outcome = self.grade(payload=payload)
        findings = {finding.code: finding for finding in outcome.findings}

        self.assertNotEqual(outcome.status, RunStatus.PASSED)
        self.assertIn("DEPLOYED_ARTEFACT_MISMATCH", findings)
        self.assertEqual(
            [
                item["artefact"]
                for item in findings["DEPLOYED_ARTEFACT_MISMATCH"].details["mismatches"]
            ],
            [FACTORY_ROLE],
        )


# ---------------------------------------------------------------------------
# The flags that carry all of this to the harness
# ---------------------------------------------------------------------------
class HarnessArgvExtrasTests(unittest.TestCase):
    def test_the_prepared_commit_is_passed_to_the_harness(self):
        """The selected Gonka and Marketplace commits are passed without a prepared SHA."""
        context = E2ERunContext(
            gonka_requested_sha=REQUESTED_SHA,
            contracts_requested_sha=MARKETPLACE_SHA,
            expected_gonka_sha=REQUESTED_SHA,
            expected_marketplace_sha=MARKETPLACE_SHA,
        )

        argv = context.harness_argv_extras()

        self.assertNotIn("--expected-gonka-prepared-sha", argv)
        self.assertIn("--expected-gonka-sha", argv)
        self.assertEqual(argv[argv.index("--expected-gonka-sha") + 1], REQUESTED_SHA)
        self.assertIn("--expected-marketplace-sha", argv)
        self.assertEqual(argv[argv.index("--expected-marketplace-sha") + 1], MARKETPLACE_SHA)

    def test_the_a9_manifest_path_is_passed_to_the_harness(self):
        manifest_path = Path("/build/a9-release/build-manifest.json")
        context = E2ERunContext(a9_manifest_path=manifest_path)

        argv = context.harness_argv_extras()

        self.assertIn("--manifest", argv)
        self.assertEqual(argv[argv.index("--manifest") + 1], str(manifest_path))

    def test_an_unset_prepared_commit_adds_no_flag(self):
        """Absent optional fields add no flags, and the immutable model is always declared."""
        context = E2ERunContext(expected_gonka_sha=REQUESTED_SHA)

        argv = context.harness_argv_extras()

        self.assertNotIn("--expected-gonka-prepared-sha", argv)
        self.assertNotIn("--manifest", argv)
        self.assertNotIn("--allowed-test-path", argv)
        self.assertNotIn("--expected-runtime", argv)
        self.assertEqual(argv[argv.index("--evidence-model") + 1], EVIDENCE_MODEL_IMMUTABLE)

    def test_the_recorded_context_dictionary_names_both_commits_and_the_manifest(self):
        manifest_path = Path("/build/a9-release/build-manifest.json")
        context = E2ERunContext(
            gonka_requested_sha=REQUESTED_SHA,
            contracts_requested_sha=MARKETPLACE_SHA,
            a9_manifest_path=manifest_path,
        )

        recorded = context.to_dict()

        self.assertEqual(recorded["gonka_requested_sha"], REQUESTED_SHA)
        self.assertEqual(recorded["contracts_requested_sha"], MARKETPLACE_SHA)
        self.assertNotIn("gonka_prepared_sha", recorded)
        self.assertEqual(recorded["a9_manifest_path"], str(manifest_path))
        self.assertEqual(recorded["evidence_model"], EVIDENCE_MODEL_IMMUTABLE)


if __name__ == "__main__":
    unittest.main()
