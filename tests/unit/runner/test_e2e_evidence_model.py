"""Tests for the current E2E evidence-model policy and its integration points.

The runner grades exactly one model: ``EVIDENCE_MODEL_IMMUTABLE``. Both product
commits are built and tested unmodified, the running binary must report the
selected commit itself, and ``source-immutability.json`` proves that neither
snapshot changed. The two older identifiers (the overlay ``legacy`` model and
the ``e2e-prepared-build`` model) remain *recognisable* so that old evidence can
be read and classified, but a declaration of either is never graded as an
immutable-source pass. Missing declarations and unknown models never receive
fallback rules.

Coverage includes:

* rejection of the legacy-shaped synthetic live-context and validation of the
  same document rewritten into the immutable-source producer's shape;
* model enforcement by the live-context and offline suite verifiers;
* real context argv generation, harness parsing, and environment readers,
  followed by verification of a synthetic document populated from those readers;
* context and runtime identity serialization, and orchestrator forwarding;
* shared model literals and inclusion of the policy file in the runner hash.

Historical-shaped input is loaded read-only from
``tests/fixtures/evidence/lock-exact-epoch-legacy-context.json``, a committed
synthetic document whose identities are derivations (see
``tests/unit/runner/support/synthetic_evidence.py``). Its source block is
pinned below. Current-model fixtures adapt that input or use synthetic
factories; suite documents also use production dataclass serializers. The
argv/environment tests do not execute the harness's live-context producer.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forward_e2e.execution.context import E2ERunContext
from forward_e2e.execution.errors import IntegrityError
from forward_e2e.execution.planner import VERIFIER_FILES, hash_runner_files
from forward_e2e.suite.evidence_model import (
    EVIDENCE_MODEL_E2E,
    EVIDENCE_MODEL_FIELD,
    EVIDENCE_MODEL_IMMUTABLE,
    EVIDENCE_MODEL_LEGACY,
    HISTORICAL_EVIDENCE_MODELS,
    KNOWN_EVIDENCE_MODELS,
    PROVENANCE_MODEL_HISTORICAL_PREPARED,
    PROVENANCE_MODEL_IMMUTABLE,
    EvidenceModelError,
    declared_evidence_model,
    is_historical_model,
    requires_observed_selected_commit,
    resolve_evidence_model,
    selected_sha_meaning,
)
from forward_e2e.suite.models import (
    ExecutionStatus,
    ProofLevel,
    SourceIdentity,
    SuitePlan,
    SuiteResult,
    TaskPlan,
    make_task_run_id,
)
from forward_e2e.suite.orchestrator import SuiteOrchestrator
from forward_e2e.suite.reporter import OfflineReporter
from forward_e2e.suite.runtime import SuiteRuntimeError, prepare_runtime_snapshot, sha256_file
from tests.unit.runner.real_fixtures import (
    GONKA_PREPARED_SHA,
    GONKA_SOURCE_SHA,
    LOCK_EXACT_E_CHECKPOINTS,
    MARKETPLACE_SHA,
    real_lock_exact_e_context_for_run,
    runtime_identity_document,
)
from tests.unit.runner.support.fakes import (
    GONKA_TREE_SHA,
    IMMUTABLE_EVIDENCE_FILES,
    MARKETPLACE_TREE_SHA,
    apply_immutable_source_fields,
    source_immutability_bytes,
)
from forward_e2e.suite.verifier import (
    verify_live_context,
    verify_suite_artifacts_integrity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# the harness is a standalone script, loaded the way scripts/tests load it
# ---------------------------------------------------------------------------
# ``scripts/acceptance_harness.py`` runs inside the runner image and under Testermint,
# where ``forward_e2e`` is not importable. It is therefore loaded by path, exactly as
# tests/unit/harness/test_acceptance_harness*.py do, so that the *real* producer functions
# are exercised here rather than a re-implementation of them.
HARNESS_PATH = REPO_ROOT / "scripts" / "acceptance_harness.py"
if str(HARNESS_PATH.parent) not in sys.path:
    sys.path.insert(0, str(HARNESS_PATH.parent))
_HARNESS_SPEC = importlib.util.spec_from_file_location("a8_acceptance", HARNESS_PATH)
assert _HARNESS_SPEC is not None and _HARNESS_SPEC.loader is not None
a8_acceptance = importlib.util.module_from_spec(_HARNESS_SPEC)
sys.modules[_HARNESS_SPEC.name] = a8_acceptance
_HARNESS_SPEC.loader.exec_module(a8_acceptance)


# ---------------------------------------------------------------------------
# the synthetic legacy-shaped live-context fixture
# ---------------------------------------------------------------------------
LEGACY_FIXTURE_FILE = (
    REPO_ROOT / "tests" / "fixtures" / "evidence" / "lock-exact-epoch-legacy-context.json"
)
LEGACY_FIXTURE_RUN_ID = "synthetic-lock-exact-epoch-legacy"

#: The ``source`` block of the legacy-shaped fixture in
#: ``tests/fixtures/evidence/lock-exact-epoch-legacy-context.json``.
#:
#: It models a pre-evidence-model live-context where ``gonka_test_harness_sha``
#: is an overlay commit while the binary reports ``gonka_sha``. The four 40-hex
#: values are the revision pins shared with ``real_fixtures.py`` and the
#: harness; the 64-hex contract digests are the document's own derivations.
#:
#: It is pinned here as a literal and checked against the file on disk by
#: :meth:`LegacyShapedFixtureTests.test_the_pinned_legacy_source_block_is_the_one_committed_on_disk`,
#: so the fixture can never drift away from the shape it tests.
LEGACY_FIXTURE_SOURCE = {
    "a9_contract_sha256": {
        "deal": "3b3bd8db62b9e870b162c19bedf9d998139845a6e289c193cfad620613626438",
        "factory": "586cfbe105db230eb5fd0c40df993e0ee4cb7b90a0248050466fb1b9ebd657d2",
    },
    "a9_manifest_sha256": "5eeb39b51ce85152fa472d3b34381525dd6504dbef63d7d1e87e163c3b74af65",
    "gonka_sha": "29a58fcf64b87967cb874b6169ce2c61e1f269b1",
    "gonka_test_harness_sha": "c5afa458de7eb8affa7020f1d0fcd4635ba1fd42",
    "marketplace_commit_sha": "f4518654e967b25d1479660115c13f67bace18df",
    "protobuf_sha": "379bebced638aeb5e6077bfd51c986f898443832",
    "runtime": {
        "cosmos_sdk": "v0.53.3-ps19-observability",
        "go": "go version go1.24.2 linux/amd64",
        "gonka_source_sha": "29a58fcf64b87967cb874b6169ce2c61e1f269b1",
        "wasmd": "v0.54.2",
        "wasmvm": "v2.2.4",
    },
    "test_contract_sha256": {
        "caller": "8b1b7bcbef6f80881b0abe137a2dd198c42ae1bb01ae52ac73634fc76f72555c",
        "cw20": "80820b5b9b8f05f589b8c134df690f97c2ef3ecb8d0888438dcf4d6c57f11fd6",
    },
}


def legacy_fixture_live_context() -> dict:
    """The legacy-shaped live-context with its on-disk test_fixture_only guard stripped in-memory."""
    payload = json.loads(LEGACY_FIXTURE_FILE.read_text(encoding="utf-8"))
    assert payload.get("test_fixture_only") is True
    payload.pop("test_fixture_only", None)
    payload.pop("synthetic_fixture", None)
    return payload


def legacy_fixture_as_immutable_live_context() -> dict:
    """Current-model baseline: the legacy-shaped fixture rewritten into the immutable-source shape."""
    payload = legacy_fixture_live_context()
    payload["source"]["gonka_source_sha"] = payload["source"]["gonka_sha"]
    payload["source"]["runtime"]["gonka_source_sha"] = payload["source"]["gonka_sha"]
    apply_immutable_source_fields(
        payload,
        gonka_sha=GONKA_SOURCE_SHA,
        marketplace_sha=MARKETPLACE_SHA,
    )
    return payload


def legacy_fixture_identity() -> SourceIdentity:
    """What the suite plan of an immutable-source run hands the verifier."""
    return SourceIdentity(
        marketplace_commit_sha=MARKETPLACE_SHA,
        gonka_commit_sha=GONKA_SOURCE_SHA,
        runner_version_hash="r" * 64,
        catalog_version_hash="c" * 64,
        marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        gonka_tree_sha=GONKA_TREE_SHA,
    )


# ---------------------------------------------------------------------------
# the E2E immutable-source shape
# ---------------------------------------------------------------------------
E2E_REQUESTED_SHA = "a" * 40
E2E_PREPARED_SHA = "b" * 40
E2E_MARKETPLACE_SHA = "c" * 40


def e2e_live_context(
    *,
    declared_model: str | None = EVIDENCE_MODEL_IMMUTABLE,
    observed_sha: str | None = E2E_REQUESTED_SHA,
    with_runtime: bool = True,
) -> dict:
    """A live-context.json in the shape ``scripts/acceptance_harness.py`` writes."""
    from tests.unit.runner.support.fakes import live_context_payload

    return live_context_payload(
        run_id="20260912100000",
        requested_sha=E2E_REQUESTED_SHA,
        prepared_sha=E2E_PREPARED_SHA,
        observed_sha=observed_sha,
        marketplace_sha=E2E_MARKETPLACE_SHA,
        with_runtime=with_runtime,
        evidence_model=declared_model,
    )


def e2e_expected_identity() -> SourceIdentity:
    """What the E2E orchestrator hands the verifier for an immutable-source build."""
    return SourceIdentity(
        marketplace_commit_sha=E2E_MARKETPLACE_SHA,
        gonka_commit_sha=E2E_REQUESTED_SHA,
        runner_version_hash="7" * 64,
        catalog_version_hash="8" * 64,
        marketplace_tree_sha=MARKETPLACE_TREE_SHA,
        gonka_tree_sha=GONKA_TREE_SHA,
    )


class ContextFileMixin:
    """Writes a live-context document where the verifier expects to find one."""

    BASELINE_GONKA_SHA = E2E_REQUESTED_SHA
    BASELINE_MARKETPLACE_SHA = E2E_MARKETPLACE_SHA

    def setUp(self):  # noqa: D102 - unittest hook
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r11em-")
        self.root = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)
        (self.root / "source-immutability.json").write_bytes(
            source_immutability_bytes(
                gonka_sha=self.BASELINE_GONKA_SHA,
                marketplace_sha=self.BASELINE_MARKETPLACE_SHA,
            )
        )
        for relpath, content in IMMUTABLE_EVIDENCE_FILES.items():
            target = self.root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def write_context(self, payload: dict, name: str = "live-context.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# 1. legacy-shaped fixture rejection and adapted E2E provenance checks
# ---------------------------------------------------------------------------
class LegacyShapedFixtureTests(ContextFileMixin, unittest.TestCase):
    """Historical-shaped evidence is rejected; adapted E2E evidence must retain provenance."""

    BASELINE_GONKA_SHA = GONKA_SOURCE_SHA
    BASELINE_MARKETPLACE_SHA = MARKETPLACE_SHA

    def verify(self, payload: dict, **kwargs):
        return verify_live_context(
            self.write_context(payload),
            expected_checkpoints=list(LOCK_EXACT_E_CHECKPOINTS),
            expected_run_id=LEGACY_FIXTURE_RUN_ID,
            expected_source_identity=legacy_fixture_identity(),
            scenario_selector="lock-exact-e",
            evidence_scopes=["lock-exact-e"],
            **kwargs,
        )

    def test_the_pinned_legacy_source_block_is_the_one_committed_on_disk(self):
        """The committed document carries exactly the pinned legacy shape, not a drifted re-invention."""
        document = legacy_fixture_live_context()

        self.assertEqual(document["source"], LEGACY_FIXTURE_SOURCE)
        self.assertEqual(document["run_id"], LEGACY_FIXTURE_RUN_ID)
        # Nothing in the document says which pipeline wrote it.
        self.assertEqual(document["schema_version"], "1.0.0")
        self.assertEqual(document["kind"], "gonka-marketplace-a8-live-context")
        self.assertNotIn(EVIDENCE_MODEL_FIELD, document["source"])
        # The fact that makes the legacy shape a downgrade: the overlay commit
        # is not the commit the running binary reports.
        self.assertEqual(document["source"]["gonka_test_harness_sha"], GONKA_PREPARED_SHA)
        self.assertEqual(document["source"]["runtime"]["gonka_source_sha"], GONKA_SOURCE_SHA)
        self.assertNotEqual(GONKA_PREPARED_SHA, GONKA_SOURCE_SHA)

    def test_the_legacy_fixture_live_context_is_rejected_as_an_unsupported_downgrade(self):
        """Under the target runner, legacy evidence lacking EVIDENCE_MODEL_IMMUTABLE is rejected."""
        ok, observed, error = self.verify(
            legacy_fixture_live_context(), e2e_evidence_expected=False
        )

        self.assertFalse(ok)
        self.assertIn("must declare", error)
        self.assertIn(EVIDENCE_MODEL_IMMUTABLE, error)

    def test_adapted_e2e_evidence_with_a_forbidden_harness_sha_is_rejected(self):
        """Provenance validation: retired prepared-build fields are refused."""
        payload = legacy_fixture_as_immutable_live_context()
        payload["source"]["gonka_test_harness_sha"] = "f" * 40

        ok, _, error = self.verify(payload, e2e_evidence_expected=False)

        self.assertFalse(ok)
        self.assertIn("gonka_test_harness_sha", error)

    def test_adapted_e2e_evidence_without_its_tree_sha_is_rejected(self):
        """Deleting a mandatory immutable-source fact is not a way around checking it."""
        payload = legacy_fixture_as_immutable_live_context()
        del payload["source"]["gonka_tree_sha"]

        ok, _, error = self.verify(payload, e2e_evidence_expected=False)

        self.assertFalse(ok)
        self.assertIn("gonka_tree_sha", error)

    def test_adapted_e2e_evidence_with_a_foreign_marketplace_commit_is_rejected(self):
        """Provenance validation: marketplace commit must match."""
        payload = legacy_fixture_as_immutable_live_context()
        payload["source"]["marketplace_commit_sha"] = "f" * 40

        ok, _, error = self.verify(payload, e2e_evidence_expected=False)

        self.assertFalse(ok)
        self.assertIn("Marketplace commit SHA mismatch in live-context.json", error)

    def test_adapted_e2e_evidence_with_a_broken_checkpoint_is_rejected(self):
        """The scenario predicates keep their contract."""
        payload = legacy_fixture_as_immutable_live_context()
        scenario = payload["scenarios"]["lock-exact-e"]
        scenario["phases"][0]["name"] = "lock_somewhere_else"

        ok, _, error = self.verify(payload, e2e_evidence_expected=False)

        self.assertFalse(ok)
        self.assertIn("Missing required checkpoints in live-context.json", error)

    def test_the_legacy_and_prepared_models_are_unconditionally_rejected_as_downgrade(self):
        """The policy decision itself, stated once and read by the verifier."""
        self.assertFalse(requires_observed_selected_commit(EVIDENCE_MODEL_LEGACY))
        self.assertFalse(requires_observed_selected_commit(EVIDENCE_MODEL_E2E))
        self.assertTrue(requires_observed_selected_commit(EVIDENCE_MODEL_IMMUTABLE))
        self.assertTrue(is_historical_model(EVIDENCE_MODEL_LEGACY))
        self.assertTrue(is_historical_model(EVIDENCE_MODEL_E2E))
        self.assertFalse(is_historical_model(EVIDENCE_MODEL_IMMUTABLE))
        for bad in (None, EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E):
            with self.subTest(declared=bad):
                with self.assertRaises(EvidenceModelError) as ctx:
                    resolve_evidence_model(declared=bad)
                self.assertEqual(ctx.exception.code, "EVIDENCE_MODEL_DOWNGRADE")

        self.assertEqual(
            resolve_evidence_model(declared=EVIDENCE_MODEL_IMMUTABLE),
            EVIDENCE_MODEL_IMMUTABLE,
        )
        self.assertEqual(
            selected_sha_meaning(),
            "the selected Gonka commit, built without modification; the running binary "
            "must report exactly this commit",
        )


# ---------------------------------------------------------------------------
# 2. an E2E package cannot be downgraded into legacy grading
# ---------------------------------------------------------------------------
def build_legacy_suite(suite_dir: Path, suite_id: str = "suite-r11-em") -> SuitePlan:
    """A complete exported legacy suite, in the layout the orchestrator writes.

    The evidence is the legacy-shaped synthetic document, the identity is the
    historical prepared-build identity, and every artifact is indexed with its
    real hash -- so the reporter's own audit runs in full and the only thing
    under examination is which grading rules it applies.
    """
    source_id = SourceIdentity(
        marketplace_commit_sha=MARKETPLACE_SHA,
        gonka_commit_sha=GONKA_SOURCE_SHA,
        runner_version_hash="r" * 64,
        catalog_version_hash="c" * 64,
        gonka_overlay_manifest_sha256=None,
        gonka_prepared_sha=GONKA_PREPARED_SHA,
    )
    task = TaskPlan(
        task_id="lock-exact-e",
        ordinal=1,
        proof_level=ProofLevel.NATIVE,
        description="Funded lock succeeds exactly at E (lower boundary)",
        scenario_selector="lock-exact-e",
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=["live-context.json"],
        expected_checkpoints=list(LOCK_EXACT_E_CHECKPOINTS),
        coverage_ids=["C1 exact E lower boundary"],
        limitations=["live network run"],
        evidence_scopes=["lock-exact-e"],
    )
    plan = SuitePlan(
        "1.0.0", suite_id, "2026-09-12T12:00:00Z", "smoke", None, source_id, [task]
    )
    (suite_dir / "suite-plan.json").write_text(
        json.dumps(plan.to_dict()), encoding="utf-8"
    )

    run_id = make_task_run_id(suite_id, 1, "lock-exact-e")
    run_dir = suite_dir / "runs" / run_id
    run_dir.mkdir(parents=True)

    context_file = run_dir / "live-context.json"
    legacy_ctx = real_lock_exact_e_context_for_run(run_id)
    legacy_ctx["source"]["evidence_model"] = EVIDENCE_MODEL_LEGACY
    context_file.write_text(
        json.dumps(legacy_ctx), encoding="utf-8"
    )
    identity_file = run_dir / "identity.json"
    identity_file.write_text(
        json.dumps(runtime_identity_document(run_id)), encoding="utf-8"
    )

    index_doc = {
        "schema_version": "1.0.0",
        "total_artifacts": 2,
        "artifacts": [
            {
                "relative_path": f"runs/{run_id}/identity.json",
                "size_bytes": identity_file.stat().st_size,
                "sha256": sha256_file(identity_file),
                "run_id": run_id,
                "task_id": "lock-exact-e",
                "artifact_kind": "METADATA_JSON",
            },
            {
                "relative_path": f"runs/{run_id}/live-context.json",
                "size_bytes": context_file.stat().st_size,
                "sha256": sha256_file(context_file),
                "run_id": run_id,
                "task_id": "lock-exact-e",
                "artifact_kind": "METADATA_JSON",
            },
        ],
    }
    (suite_dir / "artifact-index.json").write_text(
        json.dumps(index_doc), encoding="utf-8"
    )
    return plan


class E2EPackageDowngradeTests(ContextFileMixin, unittest.TestCase):
    """Inside an E2E run package the strict rules are not optional."""

    def test_an_undeclared_live_context_inside_an_e2e_package_is_rejected_as_a_downgrade(self):
        with self.assertRaises(EvidenceModelError) as raised:
            resolve_evidence_model(declared=None)
        self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_DOWNGRADE")

        # The legacy-shaped document is a perfectly good legacy file; placed
        # inside an E2E package it is an attempt to be graded by weaker rules.
        ok, _, error = verify_live_context(
            self.write_context(legacy_fixture_live_context()),
            expected_source_identity=legacy_fixture_identity(),
            e2e_evidence_expected=True,
        )

        self.assertFalse(ok)
        self.assertIn("must declare", error)
        self.assertIn(EVIDENCE_MODEL_IMMUTABLE, error)

    def test_a_legacy_or_prepared_declaration_inside_an_e2e_package_is_rejected_as_a_downgrade(self):
        for historical_model in (EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E):
            with self.subTest(historical_model=historical_model):
                with self.assertRaises(EvidenceModelError) as raised:
                    resolve_evidence_model(declared=historical_model)
                self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_DOWNGRADE")

                payload = e2e_live_context(declared_model=historical_model)
                ok, _, error = verify_live_context(
                    self.write_context(payload),
                    expected_source_identity=e2e_expected_identity(),
                    e2e_evidence_expected=True,
                )

                self.assertFalse(ok)
                self.assertIn("weaker prepared-build or legacy rules", error)

    def test_deleting_the_declaration_from_e2e_evidence_does_not_restore_the_legacy_rules(self):
        """The whole point of declaring: the check cannot be removed by omission."""
        strict = e2e_live_context(observed_sha=E2E_PREPARED_SHA)
        ok, _, error = verify_live_context(
            self.write_context(strict, name="declared.json"),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=True,
        )
        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)

        # Exactly one fact removed: the declaration. The observed commit is
        # still the wrong one, and the file must still not pass.
        stripped = copy.deepcopy(strict)
        del stripped["source"][EVIDENCE_MODEL_FIELD]
        ok, _, error = verify_live_context(
            self.write_context(stripped, name="stripped.json"),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=True,
        )

        self.assertFalse(ok)
        self.assertIn("must declare", error)

    def test_the_offline_suite_verifier_rejects_legacy_evidence(self):
        """The offline suite verifier always expects the immutable model and rejects legacy evidence."""
        suite_dir = self.root / "suite"
        suite_dir.mkdir()
        plan = build_legacy_suite(suite_dir)

        _, _, errors = verify_suite_artifacts_integrity(suite_dir, plan=plan)

        self.assertTrue(errors, "legacy evidence must not audit clean")
        joined = " | ".join(errors)
        self.assertIn("structured evidence invalid", joined)
        self.assertIn(EVIDENCE_MODEL_IMMUTABLE, joined)

    def test_the_summary_names_the_evidence_model_and_what_the_selected_sha_means(self):
        """A reader comparing SHAs by eye must be told which meaning applies."""
        suite_dir = self.root / "suite"
        suite_dir.mkdir()
        plan = build_legacy_suite(suite_dir)
        immutable_identity = SourceIdentity(
            marketplace_commit_sha=MARKETPLACE_SHA,
            gonka_commit_sha=GONKA_SOURCE_SHA,
            runner_version_hash="r" * 64,
            catalog_version_hash="c" * 64,
            marketplace_tree_sha=MARKETPLACE_TREE_SHA,
            gonka_tree_sha=GONKA_TREE_SHA,
            source_immutability_verdict="UNCHANGED",
        )
        result = SuiteResult(
            schema_version="1.0.0",
            suite_id=plan.suite_id,
            created_at_utc=plan.created_at_utc,
            completed_at_utc="2026-09-12T12:30:00Z",
            source_identity=immutable_identity,
            overall_status=ExecutionStatus.PASSED,
            tasks=[],
            summary_message="",
        )

        summary = OfflineReporter(suite_dir).build_summary_markdown(result)
        self.assertIn(f"`{EVIDENCE_MODEL_IMMUTABLE}`", summary)
        self.assertIn(selected_sha_meaning(), summary)
        self.assertNotIn(f"`{EVIDENCE_MODEL_LEGACY}`", summary)


# ---------------------------------------------------------------------------
# 3. an unknown model is an error, never a fallback
# ---------------------------------------------------------------------------
class UnknownEvidenceModelTests(ContextFileMixin, unittest.TestCase):
    """Tomorrow's evidence is not graded by today's weakest rule."""

    def test_an_unknown_model_string_is_an_error_rather_than_a_silent_fallback(self):
        unknown = EVIDENCE_MODEL_IMMUTABLE + "-experimental"

        with self.assertRaises(EvidenceModelError) as raised:
            declared_evidence_model({EVIDENCE_MODEL_FIELD: unknown})
        self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_UNKNOWN")

        # A valid current document is accepted, so the rejection below is
        # caused by the unknown value alone and not by any other broken fact.
        payload = e2e_live_context()
        ok, _, error = verify_live_context(
            self.write_context(payload, name="valid.json"),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=True,
        )
        self.assertTrue(ok, error)

        payload["source"][EVIDENCE_MODEL_FIELD] = unknown
        ok, _, error = verify_live_context(
            self.write_context(payload, name="unknown.json"),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=True,
        )

        self.assertFalse(ok)
        self.assertIn("Unknown evidence model", error)
        self.assertIn("refuses to guess", error)

    def test_an_absent_or_blank_declaration_is_silence_but_a_typo_is_not(self):
        self.assertIsNone(declared_evidence_model({}))
        self.assertIsNone(declared_evidence_model({EVIDENCE_MODEL_FIELD: None}))
        self.assertIsNone(declared_evidence_model({EVIDENCE_MODEL_FIELD: "   "}))
        self.assertIsNone(declared_evidence_model(None))
        self.assertEqual(
            declared_evidence_model({EVIDENCE_MODEL_FIELD: f" {EVIDENCE_MODEL_IMMUTABLE} "}),
            EVIDENCE_MODEL_IMMUTABLE,
        )

        # A near miss -- the shape a hand-edited or drifted producer produces --
        # is an error, not "close enough to legacy".
        for typo in ("legacy", "e2e", EVIDENCE_MODEL_LEGACY.upper(), "a8.evidence/legacy-overlay/2"):
            with self.subTest(typo=typo):
                with self.assertRaises(EvidenceModelError) as raised:
                    declared_evidence_model({EVIDENCE_MODEL_FIELD: typo})
                self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_UNKNOWN")


# ---------------------------------------------------------------------------
# 4. under the immutable model the binary must report the selected commit
# ---------------------------------------------------------------------------
class E2EObservedCommitTests(ContextFileMixin, unittest.TestCase):
    """The binary must report the selected commit itself."""

    def verify(self, payload: dict, *, e2e_evidence_expected: bool, name="live-context.json"):
        return verify_live_context(
            self.write_context(payload, name=name),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=e2e_evidence_expected,
        )

    def test_e2e_evidence_whose_binary_reports_the_selected_commit_is_accepted(self):
        ok, _, error = self.verify(e2e_live_context(), e2e_evidence_expected=True)

        self.assertIsNone(error)
        self.assertTrue(ok)

    def test_missing_or_legacy_declarations_are_rejected_without_suite_identity_or_flag(self):
        """Model declarations are mandatory independently of orchestrator context."""
        positive = e2e_live_context()
        ok, _, error = verify_live_context(
            self.write_context(positive),
            expected_source_identity=None,
            e2e_evidence_expected=False,
        )
        self.assertIsNone(error)
        self.assertTrue(ok)

        for model in (None, EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E):
            with self.subTest(declared=model):
                payload = copy.deepcopy(positive)
                if model is None:
                    del payload["source"][EVIDENCE_MODEL_FIELD]
                else:
                    payload["source"][EVIDENCE_MODEL_FIELD] = model

                ok, _, error = verify_live_context(
                    self.write_context(payload),
                    expected_source_identity=None,
                    e2e_evidence_expected=False,
                )

                self.assertFalse(ok)
                self.assertIn("weaker prepared-build or legacy rules", error)
                self.assertIn(EVIDENCE_MODEL_IMMUTABLE, error)

    def test_e2e_evidence_whose_binary_reports_another_commit_is_rejected(self):
        """One fact changed: the commit the running binary reports."""
        payload = e2e_live_context(observed_sha=E2E_PREPARED_SHA)

        ok, _, error = self.verify(payload, e2e_evidence_expected=True)

        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)
        self.assertIn(repr(E2E_PREPARED_SHA), error)

    def test_legacy_declaration_is_rejected_as_an_unsupported_downgrade(self):
        """Declaring EVIDENCE_MODEL_LEGACY is rejected as an unsupported downgrade."""
        strict = e2e_live_context(observed_sha=E2E_PREPARED_SHA)
        relaxed = copy.deepcopy(strict)
        relaxed["source"][EVIDENCE_MODEL_FIELD] = EVIDENCE_MODEL_LEGACY

        self.assertEqual(
            {k: v for k, v in strict["source"].items() if k != EVIDENCE_MODEL_FIELD},
            {k: v for k, v in relaxed["source"].items() if k != EVIDENCE_MODEL_FIELD},
        )

        ok, _, error = self.verify(strict, e2e_evidence_expected=True, name="strict.json")
        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)

        ok, _, error = self.verify(
            relaxed, e2e_evidence_expected=False, name="relaxed.json"
        )
        self.assertFalse(ok)
        self.assertIn("weaker prepared-build or legacy rules", error)

    def test_e2e_evidence_without_a_runtime_section_is_rejected(self):
        payload = e2e_live_context(with_runtime=False)

        ok, _, error = self.verify(payload, e2e_evidence_expected=True)

        self.assertFalse(ok)
        self.assertIn("runtime", error)

    def test_e2e_evidence_whose_runtime_section_has_no_commit_is_rejected(self):
        payload = e2e_live_context(observed_sha=None)

        ok, _, error = self.verify(payload, e2e_evidence_expected=True)

        self.assertFalse(ok)
        self.assertIn("gonka_source_sha", error)

    def test_e2e_evidence_is_graded_strictly_even_when_read_outside_its_package(self):
        """An exported context keeps its own rules; the package only adds them."""
        self.assertEqual(
            resolve_evidence_model(declared=EVIDENCE_MODEL_IMMUTABLE),
            EVIDENCE_MODEL_IMMUTABLE,
        )
        payload = e2e_live_context(observed_sha=E2E_PREPARED_SHA)

        ok, _, error = self.verify(payload, e2e_evidence_expected=False)

        self.assertFalse(ok)
        self.assertIn("Observed runtime commit mismatch in live-context.json", error)


# ---------------------------------------------------------------------------
# 5. context argv and harness environment handover
# ---------------------------------------------------------------------------
class HarnessExpectationHandoverTests(ContextFileMixin, unittest.TestCase):
    """Real argv/environment handover, then verification of synthetic evidence."""

    ENV_NAMES = (
        a8_acceptance.ENV_EVIDENCE_MODEL,
        a8_acceptance.ENV_EXPECTED_GONKA_SHA,
        a8_acceptance.ENV_EXPECTED_PROTO_SHA,
        a8_acceptance.ENV_EXPECTED_RUNTIME,
        *a8_acceptance.LEGACY_OVERLAY_ENVIRONMENT,
    )

    def setUp(self):
        super().setUp()
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in self.ENV_NAMES:
            os.environ.pop(name, None)

    def e2e_context(self) -> E2ERunContext:
        return E2ERunContext(
            gonka_requested_sha=E2E_REQUESTED_SHA,
            expected_gonka_sha=E2E_REQUESTED_SHA,
        )

    def parse_run_live(self, extras):
        base = ["run-live", "--gonka-dir", str(self.root)]
        if "--expected-gonka-sha" not in extras:
            base.extend(["--expected-gonka-sha", E2E_REQUESTED_SHA])
        return a8_acceptance.parser().parse_args(base + list(extras))

    def test_the_e2e_run_context_declares_the_immutable_model_on_the_harness_command_line(self):
        argv = self.e2e_context().harness_argv_extras()

        self.assertIn("--evidence-model", argv)
        self.assertEqual(argv[argv.index("--evidence-model") + 1], EVIDENCE_MODEL_IMMUTABLE)

        # An empty context declaration emits no flag. The harness defaults
        # to EVIDENCE_MODEL_IMMUTABLE, as checked by the no-flag invocation test below.
        silent = E2ERunContext(evidence_model="")
        self.assertNotIn("--evidence-model", silent.harness_argv_extras())

    def test_context_expectations_reach_harness_readers_and_validate_synthetic_evidence(self):
        args = self.parse_run_live(self.e2e_context().harness_argv_extras())
        self.assertEqual(args.evidence_model, EVIDENCE_MODEL_IMMUTABLE)

        exported = a8_acceptance.apply_expectation_overrides(args)

        self.assertEqual(exported[a8_acceptance.ENV_EVIDENCE_MODEL], EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(
            os.environ[a8_acceptance.ENV_EVIDENCE_MODEL], EVIDENCE_MODEL_IMMUTABLE
        )
        self.assertEqual(a8_acceptance.evidence_model(), EVIDENCE_MODEL_IMMUTABLE)

        # Populate a synthetic document from the real environment readers.
        # This checks value compatibility, not the harness serialization path.
        payload = e2e_live_context(declared_model=a8_acceptance.evidence_model())
        payload["source"]["gonka_source_sha"] = a8_acceptance.expected_gonka_sha()
        payload["source"]["gonka_sha"] = a8_acceptance.expected_gonka_sha()
        payload["source"]["runtime"]["gonka_source_sha"] = a8_acceptance.expected_gonka_sha()

        self.assertEqual(payload["source"]["gonka_source_sha"], E2E_REQUESTED_SHA)
        self.assertEqual(
            declared_evidence_model(payload["source"]), EVIDENCE_MODEL_IMMUTABLE
        )
        self.assertEqual(
            resolve_evidence_model(
                declared=declared_evidence_model(payload["source"])
            ),
            EVIDENCE_MODEL_IMMUTABLE,
        )

        ok, _, error = verify_live_context(
            self.write_context(payload),
            expected_source_identity=e2e_expected_identity(),
            e2e_evidence_expected=True,
        )
        self.assertIsNone(error)
        self.assertTrue(ok)

    def test_invocation_without_flag_defaults_to_immutable_model_and_rejects_historical_models(self):
        args = self.parse_run_live([])
        self.assertEqual(args.evidence_model, EVIDENCE_MODEL_IMMUTABLE)

        exported = a8_acceptance.apply_expectation_overrides(args)

        self.assertEqual(exported[a8_acceptance.ENV_EVIDENCE_MODEL], EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(os.environ[a8_acceptance.ENV_EVIDENCE_MODEL], EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(a8_acceptance.evidence_model(), EVIDENCE_MODEL_IMMUTABLE)

        for historical_model in (EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_E2E):
            with self.subTest(historical_model=historical_model):
                hist_args = self.parse_run_live(["--evidence-model", historical_model])
                with self.assertRaises((SystemExit, a8_acceptance.AcceptanceError)):
                    a8_acceptance.apply_expectation_overrides(hist_args)

                os.environ[a8_acceptance.ENV_EVIDENCE_MODEL] = historical_model
                with self.assertRaises((SystemExit, a8_acceptance.AcceptanceError)):
                    a8_acceptance.evidence_model()

    def test_an_unknown_model_on_the_command_line_fails_before_any_evidence_is_written(self):
        args = self.parse_run_live(["--evidence-model", "a8.evidence/legacy-overlay/2"])

        with self.assertRaises((SystemExit, a8_acceptance.AcceptanceError)):
            a8_acceptance.apply_expectation_overrides(args)

        self.assertNotIn(a8_acceptance.ENV_EVIDENCE_MODEL, os.environ)

    def test_an_unknown_model_in_the_environment_is_refused_when_it_is_read_back(self):
        os.environ[a8_acceptance.ENV_EVIDENCE_MODEL] = "a8.evidence/whatever/1"

        with self.assertRaises((SystemExit, a8_acceptance.AcceptanceError)):
            a8_acceptance.evidence_model()


# ---------------------------------------------------------------------------
# 6. the deliberate duplication of the literals is pinned
# ---------------------------------------------------------------------------
class DuplicatedLiteralsArePinnedTests(unittest.TestCase):
    """Three files spell these strings out; they must spell them identically."""

    def test_the_acceptance_harness_literals_equal_the_shared_evidence_model_module(self):
        self.assertEqual(a8_acceptance.EVIDENCE_MODEL_LEGACY, EVIDENCE_MODEL_LEGACY)
        self.assertEqual(a8_acceptance.EVIDENCE_MODEL_E2E, EVIDENCE_MODEL_E2E)
        self.assertEqual(a8_acceptance.EVIDENCE_MODEL_IMMUTABLE, EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(
            set(a8_acceptance.KNOWN_EVIDENCE_MODELS), set(KNOWN_EVIDENCE_MODELS)
        )
        self.assertEqual(a8_acceptance.ENV_EVIDENCE_MODEL, "E2E_EVIDENCE_MODEL")
        self.assertEqual(a8_acceptance.LEGACY_ENV_EVIDENCE_MODEL, "A8_EVIDENCE_MODEL")

        # What drift would look like, and that it cannot pass unnoticed: a value
        # the shared module does not know is an error on the consumer side.
        with self.assertRaises(EvidenceModelError) as raised:
            declared_evidence_model(
                {EVIDENCE_MODEL_FIELD: a8_acceptance.EVIDENCE_MODEL_IMMUTABLE + "/3"}
            )
        self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_UNKNOWN")

    def test_the_e2e_run_context_literal_equals_the_shared_evidence_model_module(self):
        self.assertEqual(E2ERunContext.E2E_EVIDENCE_MODEL, EVIDENCE_MODEL_IMMUTABLE)

        context = E2ERunContext()
        self.assertEqual(context.evidence_model, EVIDENCE_MODEL_IMMUTABLE)
        self.assertIn(context.evidence_model, KNOWN_EVIDENCE_MODELS)
        self.assertEqual(context.to_dict()[EVIDENCE_MODEL_FIELD], EVIDENCE_MODEL_IMMUTABLE)

        # A drifted context declares something the verifier cannot grade, so the
        # run fails loudly instead of being graded by the legacy rules.
        drifted = E2ERunContext(evidence_model=EVIDENCE_MODEL_IMMUTABLE + "-drift")
        argv = drifted.harness_argv_extras()
        declared = argv[argv.index("--evidence-model") + 1]
        with self.assertRaises(EvidenceModelError) as raised:
            declared_evidence_model({EVIDENCE_MODEL_FIELD: declared})
        self.assertEqual(raised.exception.code, "EVIDENCE_MODEL_UNKNOWN")

    def test_the_context_dictionary_uses_the_shared_evidence_model_field_name(self):
        self.assertEqual(EVIDENCE_MODEL_FIELD, "evidence_model")
        self.assertIn(EVIDENCE_MODEL_FIELD, E2ERunContext().to_dict())


# ---------------------------------------------------------------------------
# 7. the runtime identity and the orchestrator that fills it
# ---------------------------------------------------------------------------
class RuntimeIdentityDeclaresTheModelTests(unittest.TestCase):
    """``identity.json`` states the immutable-source model and selected commit."""

    HEAD_SHA = "e" * 40

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r11em-")
        self.root = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)
        self.marketplace = self.root / "marketplace"
        self.gonka = self.root / "gonka"
        self.runtime_root = self.root / "runtime"
        for directory in (self.marketplace, self.gonka, self.runtime_root):
            directory.mkdir(parents=True)

    def prepare(self, run_id: str, **kwargs) -> dict:
        """Run the real ``prepare_runtime_snapshot``; only git/OS calls are faked."""

        def fake_bundle(repo_dir, bundle_output):
            path = Path(bundle_output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"bundle of " + str(repo_dir).encode("utf-8"))
            return self.HEAD_SHA

        def fake_clone(bundle_path, target_dir, expected_head):
            Path(target_dir).mkdir(parents=True, exist_ok=True)

        with patch("forward_e2e.suite.runtime.check_native_filesystem"), patch(
            "forward_e2e.suite.runtime.perform_mount_probe"
        ), patch("forward_e2e.suite.runtime.verify_toolchain_versions", return_value={}), patch(
            "forward_e2e.suite.runtime.get_git_clean_head", return_value=self.HEAD_SHA
        ), patch(
            "forward_e2e.suite.runtime.git_tree_sha",
            side_effect=[GONKA_TREE_SHA, MARKETPLACE_TREE_SHA],
        ), patch(
            "forward_e2e.suite.runtime.create_git_bundle", side_effect=fake_bundle
        ), patch(
            "forward_e2e.suite.runtime.clone_from_bundle", side_effect=fake_clone
        ):
            snapshot = prepare_runtime_snapshot(
                marketplace_source=self.marketplace,
                gonka_source=self.gonka,
                run_id=run_id,
                runtime_root=self.runtime_root,
                **kwargs,
            )
        return json.loads(snapshot.identity_file.read_text(encoding="utf-8"))

    def test_a_snapshot_whose_caller_declares_nothing_records_the_immutable_model(self):
        identity = self.prepare("r11-default")

        self.assertEqual(identity[EVIDENCE_MODEL_FIELD], EVIDENCE_MODEL_IMMUTABLE)

    def test_a_snapshot_records_the_model_its_caller_declares(self):
        identity = self.prepare("r11-immutable", evidence_model=EVIDENCE_MODEL_IMMUTABLE)

        self.assertEqual(identity[EVIDENCE_MODEL_FIELD], EVIDENCE_MODEL_IMMUTABLE)

    def test_snapshot_records_immutable_source_policy_and_rejects_mismatched_source_sha(self):
        """The snapshot records immutable source policy and refuses a different selected SHA."""
        identity = self.prepare("r11-no-overlay")

        self.assertEqual(identity["source_policy"], "immutable")
        self.assertNotIn("gonka_prepared_sha", identity)
        self.assertEqual(identity[EVIDENCE_MODEL_FIELD], EVIDENCE_MODEL_IMMUTABLE)
        with self.assertRaises(SuiteRuntimeError):
            self.prepare("r11-mismatch", recorded_gonka_source_sha="f" * 40)


class OrchestratorForwardsTheModelTests(unittest.TestCase):
    """The suite runner states the model instead of letting the verifier guess."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r11em-")
        self.root = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)
        self.marketplace = self.root / "marketplace"
        self.gonka = self.root / "gonka"
        self.output_dir = self.root / "output"
        self.runtime_root = self.root / "runtime"
        for directory in (self.marketplace, self.gonka, self.output_dir, self.runtime_root):
            directory.mkdir(parents=True)

    def run_failing_suite(self, *, suite_id: str, e2e_context) -> dict:
        """Drive the real ``run_suite`` until the first task gives up."""
        from tests.unit.runner.support.fakes import run_failing_suite_orchestrator

        captured, _ = run_failing_suite_orchestrator(
            marketplace_dir=self.marketplace,
            gonka_dir=self.gonka,
            output_dir=self.output_dir,
            runtime_root=self.runtime_root,
            suite_id=suite_id,
            e2e_context=e2e_context,
            clean_heads=[MARKETPLACE_SHA, GONKA_SOURCE_SHA],
        )
        return captured

    def test_an_e2e_suite_forwards_its_context_model_and_demands_e2e_evidence(self):
        captured = self.run_failing_suite(
            suite_id="suite-r11-e2e",
            e2e_context=E2ERunContext(
                gonka_requested_sha=GONKA_SOURCE_SHA,
            ),
        )

        self.assertEqual(
            captured["prepare"]["evidence_model"], EVIDENCE_MODEL_IMMUTABLE
        )
        self.assertTrue(captured["evaluate"]["e2e_evidence_expected"])

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


# ---------------------------------------------------------------------------
# 8. the grading policy is part of the runner identity in the lock
# ---------------------------------------------------------------------------
class GradingPolicyIsHashedIntoTheLockTests(unittest.TestCase):
    """Changing what "passed" means must change the verifier hash."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-r11em-")
        self.root = Path(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)

    def make_runner_root(self) -> Path:
        root = self.root / "runner"
        for rel in VERIFIER_FILES:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# stand-in for {rel}\n", encoding="utf-8")
        return root

    def test_the_evidence_model_policy_belongs_to_the_hashed_verifier_files(self):
        self.assertIn("forward_e2e/suite/evidence_model.py", VERIFIER_FILES)
        self.assertTrue((REPO_ROOT / "forward_e2e" / "suite" / "evidence_model.py").is_file())

    def test_editing_the_evidence_model_policy_changes_the_verifier_hash(self):
        root = self.make_runner_root()
        before = hash_runner_files(root, VERIFIER_FILES)

        policy = root / "forward_e2e" / "suite" / "evidence_model.py"
        policy.write_text(
            "# the legacy model now waives the harness SHA too\n", encoding="utf-8"
        )
        after = hash_runner_files(root, VERIFIER_FILES)

        self.assertNotEqual(before, after)

    def test_deleting_the_evidence_model_policy_is_an_error_not_a_shorter_hash(self):
        root = self.make_runner_root()
        (root / "forward_e2e" / "suite" / "evidence_model.py").unlink()

        with self.assertRaises(IntegrityError):
            hash_runner_files(root, VERIFIER_FILES)


if __name__ == "__main__":
    unittest.main()
