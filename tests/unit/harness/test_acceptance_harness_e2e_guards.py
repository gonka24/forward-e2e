"""Guard tests for the env-parameterised provenance expectations in the harness.

The expectation helpers require an explicit plan SHA for the Gonka target,
preserve the independent ABI/protobuf pin default, refuse the removed
overlay/prepared-tree variables, and enforce strict runtime provenance checks.
The snapshot check itself is exercised in ``test_acceptance_harness_run_live.py``.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import argparse
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


try:
    from tests.unit.harness.support import (
        EXPECTATION_ENV_NAMES,
        a8,
        version_output,
    )
except ImportError:
    from support import (
        EXPECTATION_ENV_NAMES,
        a8,
        version_output,
    )

SELECTED_SHA = "b" * 40
SELECTED_BASE_SHA = "c" * 40
UNRELATED_SHA = "d" * 40
HISTORICAL_GONKA_SHAS = (
    "29a58fcf64b87967cb874b6169ce2c61e1f269b1",
    "379bebced638aeb5e6077bfd51c986f898443832",
)
ADAPTER_TEST_PATH = "inference-chain/app/a8_e2e_probe_test.go"


class ExpectationHelperDefaultTests(unittest.TestCase):
    """Without plan environment variables, Gonka SHA helpers must fail closed."""

    def setUp(self):
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in EXPECTATION_ENV_NAMES:
            os.environ.pop(name, None)

    def test_missing_gonka_expectations_fail_closed_while_proto_pin_has_reviewed_default(self):
        self.assertFalse(hasattr(a8, "EXPECTED_GONKA_SHA"))
        self.assertFalse(hasattr(a8, "EXPECTED_GONKA_BASE_SHA"))
        self.assertFalse(hasattr(a8, "A8_RUNTIME_ADAPTER_BLOBS"))
        self.assertFalse(hasattr(a8, "PR4_MANUAL_WORKFLOW_BLOBS"))
        self.assertFalse(hasattr(a8, "wsl_path"))

        with self.assertRaisesRegex(a8.AcceptanceError, a8.ENV_EXPECTED_GONKA_SHA):
            a8.expected_gonka_sha()
        with self.assertRaisesRegex(a8.AcceptanceError, a8.ENV_EXPECTED_GONKA_SHA):
            a8.expected_runtime_versions()

        self.assertEqual(a8.expected_proto_sha(), a8.EXPECTED_PROTO_SHA)
        for removed in ("expected_gonka_base_sha", "expected_gonka_prepared_sha", "extra_allowed_test_paths"):
            self.assertFalse(hasattr(a8, removed), removed)

    def test_environment_override_supplies_the_expected_gonka_sha(self):
        os.environ[a8.ENV_EXPECTED_GONKA_SHA] = SELECTED_SHA
        self.assertEqual(a8.expected_gonka_sha(), SELECTED_SHA)
        self.assertEqual(
            a8.expected_runtime_versions(),
            {
                "gonka_source_sha": SELECTED_SHA,
                "wasmd": a8.EXPECTED_WASMD_VERSION,
                "wasmvm": a8.EXPECTED_WASMVM_VERSION,
            },
        )

    def test_an_invalid_expected_sha_is_rejected_instead_of_being_ignored(self):
        for bad in ("not-a-sha", "b" * 39, "b" * 41, "g" * 40):
            with self.subTest(value=bad):
                os.environ[a8.ENV_EXPECTED_GONKA_SHA] = bad
                with self.assertRaises(SystemExit) as cm:
                    a8.expected_gonka_sha()
                self.assertIn("must be a full 40-hex commit SHA", str(cm.exception))
                self.assertIn(a8.ENV_EXPECTED_GONKA_SHA, str(cm.exception))

    def test_expected_proto_sha_is_not_replaced_by_the_selected_gonka_sha(self):
        os.environ[a8.ENV_EXPECTED_GONKA_SHA] = SELECTED_SHA

        self.assertEqual(a8.expected_gonka_sha(), SELECTED_SHA)
        self.assertEqual(a8.expected_proto_sha(), a8.EXPECTED_PROTO_SHA)
        self.assertNotEqual(a8.expected_proto_sha(), a8.expected_gonka_sha())

        os.environ[a8.ENV_EXPECTED_PROTO_SHA] = UNRELATED_SHA
        self.assertEqual(a8.expected_proto_sha(), UNRELATED_SHA)

    def test_each_removed_overlay_variable_is_refused_rather_than_ignored(self):
        for name in a8.LEGACY_OVERLAY_ENVIRONMENT:
            with self.subTest(variable=name):
                os.environ[name] = ADAPTER_TEST_PATH
                with self.assertRaisesRegex(a8.AcceptanceError, name):
                    a8.refuse_legacy_overlay_environment()
                del os.environ[name]
        a8.refuse_legacy_overlay_environment()

    def test_a_historical_evidence_model_can_no_longer_be_declared(self):
        for model in a8.HISTORICAL_EVIDENCE_MODELS:
            with self.subTest(model=model):
                os.environ[a8.ENV_EVIDENCE_MODEL] = model
                with self.assertRaises(SystemExit):
                    a8.evidence_model()
        os.environ.pop(a8.ENV_EVIDENCE_MODEL)
        self.assertEqual(a8.evidence_model(), a8.EVIDENCE_MODEL_IMMUTABLE)

    def test_expected_runtime_versions_merges_explicit_field_overrides(self):
        os.environ[a8.ENV_EXPECTED_GONKA_SHA] = SELECTED_SHA
        os.environ[a8.ENV_EXPECTED_RUNTIME] = "wasmd=v0.55.0,wasmvm=v2.3.0"
        expected = a8.expected_runtime_versions()
        self.assertEqual(expected["wasmd"], "v0.55.0")
        self.assertEqual(expected["wasmvm"], "v2.3.0")
        self.assertEqual(expected["gonka_source_sha"], SELECTED_SHA)


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in EXPECTATION_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ[a8.ENV_EXPECTED_GONKA_SHA] = SELECTED_SHA

    def test_parse_runtime_identity_uses_the_parameterised_expectations(self):
        fields = a8.parse_runtime_identity(version_output(commit=SELECTED_SHA))
        self.assertEqual(fields["gonka_source_sha"], SELECTED_SHA)
        self.assertEqual(fields["wasmd"], a8.EXPECTED_WASMD_VERSION)
        self.assertEqual(fields["wasmvm"], a8.EXPECTED_WASMVM_VERSION)

    def test_parse_runtime_identity_rejects_a_wrong_wasmd_or_wasmvm_version(self):
        with self.assertRaises(a8.AcceptanceError) as cm:
            a8.parse_runtime_identity(
                version_output(commit=SELECTED_SHA, wasmd="v0.53.0")
            )
        self.assertIn("running Gonka runtime provenance mismatch", str(cm.exception))
        self.assertIn("wasmd", str(cm.exception))

        with self.assertRaises(a8.AcceptanceError) as cm:
            a8.parse_runtime_identity(
                version_output(commit=SELECTED_SHA, wasmvm="v2.0.0")
            )
        self.assertIn("wasmvm", str(cm.exception))

    def test_parse_runtime_identity_rejects_a_binary_reporting_another_commit(self):
        for wrong_commit in (UNRELATED_SHA, *HISTORICAL_GONKA_SHAS):
            with self.subTest(wrong_commit=wrong_commit):
                with self.assertRaises(a8.AcceptanceError) as cm:
                    a8.parse_runtime_identity(version_output(commit=wrong_commit))
                self.assertIn("gonka_source_sha", str(cm.exception))


class ExpectationOverrideFlagTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for name in EXPECTATION_ENV_NAMES:
            os.environ.pop(name, None)

    def test_run_live_exposes_the_explicit_sha_provenance_flags(self):
        args = a8.parser().parse_args(
            [
                "run-live",
                "--gonka-dir",
                "gonka",
                "--expected-gonka-sha",
                SELECTED_SHA,
                "--expected-marketplace-sha",
                SELECTED_BASE_SHA,
                "--expected-proto-sha",
                UNRELATED_SHA,
                "--expected-runtime",
                "wasmd=v0.55.0",
                "--expected-runtime",
                "wasmvm=v2.3.0",
            ]
        )
        self.assertEqual(args.expected_gonka_sha, SELECTED_SHA)
        self.assertEqual(args.expected_marketplace_sha, SELECTED_BASE_SHA)
        self.assertEqual(args.expected_proto_sha, UNRELATED_SHA)
        self.assertEqual(args.expected_runtime, ["wasmd=v0.55.0", "wasmvm=v2.3.0"])

    def test_apply_expectation_overrides_maps_the_flags_onto_the_environment(self):
        args = argparse.Namespace(
            expected_gonka_sha=SELECTED_SHA,
            expected_proto_sha=UNRELATED_SHA,
            expected_runtime=["wasmd=v0.55.0", "wasmvm=v2.3.0"],
        )

        exported = a8.apply_expectation_overrides(args)

        self.assertEqual(os.environ[a8.ENV_EXPECTED_GONKA_SHA], SELECTED_SHA)
        self.assertEqual(os.environ[a8.ENV_EXPECTED_PROTO_SHA], UNRELATED_SHA)
        for name in a8.LEGACY_OVERLAY_ENVIRONMENT:
            self.assertNotIn(name, os.environ)
        self.assertEqual(os.environ[a8.ENV_EXPECTED_RUNTIME], "wasmd=v0.55.0,wasmvm=v2.3.0")
        self.assertEqual(exported[a8.ENV_EXPECTED_GONKA_SHA], SELECTED_SHA)

        # And the downstream helpers now see exactly those values.
        self.assertEqual(a8.expected_gonka_sha(), SELECTED_SHA)
        self.assertEqual(a8.expected_proto_sha(), UNRELATED_SHA)
        self.assertEqual(a8.expected_runtime_versions()["wasmd"], "v0.55.0")

    def test_a_plain_invocation_exports_nothing_and_requires_explicit_gonka_sha(self):
        args = argparse.Namespace(
            expected_gonka_sha=None,
            expected_proto_sha=None,
            expected_runtime=[],
        )

        self.assertEqual(a8.apply_expectation_overrides(args), {})

        for name in EXPECTATION_ENV_NAMES:
            self.assertNotIn(name, os.environ)
        with self.assertRaisesRegex(a8.AcceptanceError, a8.ENV_EXPECTED_GONKA_SHA):
            a8.expected_gonka_sha()
        self.assertEqual(a8.expected_proto_sha(), a8.EXPECTED_PROTO_SHA)

    def test_a_malformed_expected_runtime_pair_is_rejected(self):
        args = SimpleNamespace(
            expected_gonka_sha=None,
            expected_proto_sha=None,
            expected_runtime=["wasmd"],
        )

        with self.assertRaises(a8.AcceptanceError) as cm:
            a8.apply_expectation_overrides(args)
        self.assertIn("--expected-runtime expects FIELD=VALUE", str(cm.exception))
        self.assertNotIn(a8.ENV_EXPECTED_RUNTIME, os.environ)


if __name__ == "__main__":
    unittest.main()
