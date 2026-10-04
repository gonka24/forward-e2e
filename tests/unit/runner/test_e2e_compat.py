"""Unit tests for forward_e2e.execution.compat: marker-based adapter selection, scenario
support, the immutable-source policy (no patch or overlay can be declared) and
the build recipe that builds the selected Gonka commit without writing into it.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
import tempfile
import unittest

from forward_e2e.execution import compat
from forward_e2e.execution.compat import (
    INFERENCED_CONTEXT_RELPATH,
    MARKETPLACE_QUERY_CAPABILITIES,
    MOCK_SERVER_CONTEXT_RELPATH,
    MOCK_SERVER_JAR_RELPATH,
    WASM_ALLOWLIST_FILE,
    CompatibilityAdapter,
    ExpectationKind,
    assert_scenarios_supported,
    contracts_adapters,
    gonka_adapters,
    gonka_immutable_source_adapter,
    marketplace_contracts_adapter,
    measure_go_module_version,
    measured_capabilities,
    select_adapter,
)
from forward_e2e.execution.errors import UnsupportedCompatibilityError
from tests.unit.runner.support.fakes import (
    write_contracts_tree,
    write_gonka_tree,
    write_text_file,
)

#: Where the runner-owned Kotlin scenarios live now. They used to be copied
#: into the Gonka checkout from ``gonka-overlay/testermint``; they are compiled
#: against the unmodified upstream Testermint instead and never enter a
#: snapshot.
KOTLIN_ACCEPTANCE_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "harness/testermint/src/test/kotlin/MarketplaceContractAcceptanceTests.kt"
)

ADAPTER_ID = "gonka-immutable-source-v2"

# ---------------------------------------------------------------------------
# synthetic source fixtures
# ---------------------------------------------------------------------------
#: A legacy.go that already exposes all four marketplace queries, i.e. the
#: shape produced by upstream e86e4899bd8cf52d1ad4766c811f65230b2f9296.
UPSTREAM_LEGACY_GO = """package app

// AcceptedQueries is the wasm gRPC query allow-list.
var AcceptedQueries = []string{
\t"/inference.inference.Query/GetCurrentEpoch",
\t"/inference.inference.Query/ListClaimRecipients",
\t"/inference.inference.Query/EpochPerformanceSummaryByParticipant",
\t"/inference.streamvesting.Query/TotalVestingAmount",
}
"""

#: A legacy.go from before the upstream fix: no marketplace queries at all.
PRE_FIX_LEGACY_GO = """package app

var AcceptedQueries = []string{
\t"/cosmos.bank.v1beta1.Query/Balance",
}
"""

GO_MOD = """module github.com/product-science/inferenced

go 1.23.6

require (
\tgithub.com/CosmWasm/wasmd v0.53.0
\tgithub.com/CosmWasm/wasmvm/v2 v2.1.2
)
"""

#: Not in any adapter's ``verified_commits``: the point of the marker design is
#: that such a commit still resolves.
UNLISTED_SHA = "1111111111111111111111111111111111111111"
VERIFIED_UPSTREAM_SHA = "e86e4899bd8cf52d1ad4766c811f65230b2f9296"
VERIFIED_LEGACY_SHA = "379bebced638aeb5e6077bfd51c986f898443832"
VERIFIED_CONTRACTS_SHA = "7497304e5dc6bf48accdd8c91549bc22de6997fc"


class AdapterSelectionByMeasuredMarkersTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-compat-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.adapters = gonka_adapters()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tree_exposing_marketplace_queries_selects_the_upstream_adapter(self):
        tree = write_gonka_tree(self.root / "upstream", legacy_go=UPSTREAM_LEGACY_GO)

        match = select_adapter(
            self.adapters, root=tree, commit_sha=VERIFIED_UPSTREAM_SHA, role="gonka"
        )

        self.assertEqual(match.adapter.adapter_id, ADAPTER_ID)
        self.assertEqual(match.adapter.declaration()["source_policy"], "immutable")
        self.assertEqual(
            match.measurements["exposed_marketplace_queries"],
            [name for name, _ in MARKETPLACE_QUERY_CAPABILITIES],
        )

    def test_tree_without_marketplace_queries_is_rejected_with_unsupported_compatibility_error(self):
        tree = write_gonka_tree(self.root / "legacy", legacy_go=PRE_FIX_LEGACY_GO)

        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            select_adapter(
                self.adapters, root=tree, commit_sha=VERIFIED_LEGACY_SHA, role="gonka"
            )

        self.assertEqual(cm.exception.code, "UNSUPPORTED_COMPATIBILITY")
        reasons = cm.exception.details["rejected"][ADAPTER_ID]
        self.assertTrue(
            any("does not expose the marketplace queries" in reason for reason in reasons)
        )

    def test_unlisted_commit_with_matching_layout_still_resolves_via_markers(self):
        tree = write_gonka_tree(self.root / "unlisted", legacy_go=UPSTREAM_LEGACY_GO)
        self.assertNotIn(
            UNLISTED_SHA,
            set().union(*[a.verified_commits for a in self.adapters]),
        )

        match = select_adapter(
            self.adapters, root=tree, commit_sha=UNLISTED_SHA, role="gonka"
        )

        self.assertEqual(match.adapter.adapter_id, ADAPTER_ID)
        self.assertEqual(match.match_mode, "markers")

    def test_allow_listed_commit_only_upgrades_match_mode_to_verified_commit(self):
        tree = write_gonka_tree(self.root / "verified", legacy_go=UPSTREAM_LEGACY_GO)

        verified = select_adapter(
            self.adapters, root=tree, commit_sha=VERIFIED_UPSTREAM_SHA, role="gonka"
        )
        unlisted = select_adapter(
            self.adapters, root=tree, commit_sha=UNLISTED_SHA, role="gonka"
        )

        self.assertEqual(verified.match_mode, "verified-commit")
        self.assertEqual(unlisted.match_mode, "markers")
        # The same adapter, the same recipe: only the label differs.
        self.assertEqual(verified.adapter.adapter_id, unlisted.adapter.adapter_id)
        self.assertEqual(
            verified.adapter.content_hash(), unlisted.adapter.content_hash()
        )

    def test_uppercase_commit_sha_is_still_recognised_as_verified(self):
        tree = write_gonka_tree(self.root / "upper", legacy_go=UPSTREAM_LEGACY_GO)

        match = select_adapter(
            self.adapters,
            root=tree,
            commit_sha=VERIFIED_UPSTREAM_SHA.upper(),
            role="gonka",
        )

        self.assertEqual(match.match_mode, "verified-commit")

    def test_tree_matching_no_adapter_raises_unsupported_with_per_adapter_reasons(self):
        empty = self.root / "unknown-layout"
        empty.mkdir(parents=True)

        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            select_adapter(self.adapters, root=empty, commit_sha=UNLISTED_SHA, role="gonka")

        self.assertIn(
            "No compatibility adapter can drive the selected gonka sources",
            str(cm.exception),
        )
        self.assertEqual(cm.exception.code, "UNSUPPORTED_COMPATIBILITY")
        details = cm.exception.details
        self.assertEqual(details["role"], "gonka")
        self.assertEqual(details["commit_sha"], UNLISTED_SHA)
        self.assertEqual(
            sorted(details["rejected"]),
            [ADAPTER_ID],
        )
        reasons = details["rejected"][ADAPTER_ID]
        self.assertTrue(any("Makefile is missing" in reason for reason in reasons))
        self.assertTrue(
            any(
                "inference-chain/app/legacy.go is missing" in reason
                and "the wasm query allow-list lives here" in reason
                for reason in reasons
            )
        )

    def test_gonka_tree_with_half_the_layout_is_rejected_rather_than_guessed(self):
        partial = self.root / "partial"
        write_text_file(partial / WASM_ALLOWLIST_FILE, UPSTREAM_LEGACY_GO)
        write_text_file(partial / "Makefile", "all:\n")

        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            select_adapter(self.adapters, root=partial, commit_sha=UNLISTED_SHA, role="gonka")

        self.assertIn("will not guess a build recipe for an unknown layout", str(cm.exception))

    def test_contracts_workspace_layout_selects_the_marketplace_contracts_adapter(self):
        tree = write_contracts_tree(self.root / "contracts")

        unlisted = select_adapter(
            contracts_adapters(), root=tree, commit_sha=UNLISTED_SHA, role="contracts"
        )
        verified = select_adapter(
            contracts_adapters(),
            root=tree,
            commit_sha=VERIFIED_CONTRACTS_SHA,
            role="contracts",
        )

        self.assertEqual(unlisted.adapter.adapter_id, "contracts-marketplace-workspace-v1")
        self.assertEqual(unlisted.match_mode, "markers")
        self.assertEqual(verified.match_mode, "verified-commit")
        # ``exposed_marketplace_queries`` is a gonka-only measurement.
        self.assertEqual(unlisted.measurements["exposed_marketplace_queries"], [])

    def test_contracts_tree_without_the_release_script_uses_the_runner_owned_builder(self):
        tree = write_contracts_tree(self.root / "no-release", with_release_script=False)

        match = select_adapter(
            contracts_adapters(), root=tree, commit_sha=UNLISTED_SHA, role="contracts"
        )

        self.assertEqual(match.adapter.adapter_id, "contracts-marketplace-workspace-v1")

    def test_measured_capabilities_reports_exactly_what_the_tree_exposes(self):
        upstream = write_gonka_tree(self.root / "caps-yes", legacy_go=UPSTREAM_LEGACY_GO)
        pre_fix = write_gonka_tree(self.root / "caps-no", legacy_go=PRE_FIX_LEGACY_GO)
        partial_text = (
            'package app\nvar q = "/inference.inference.Query/GetCurrentEpoch"\n'
        )
        partial = write_gonka_tree(self.root / "caps-some", legacy_go=partial_text)

        self.assertEqual(
            measured_capabilities(upstream),
            ["current_epoch", "claim_recipients", "epoch_performance_summary", "total_vesting"],
        )
        self.assertEqual(measured_capabilities(pre_fix), [])
        self.assertEqual(measured_capabilities(partial), ["current_epoch"])
        self.assertEqual(measured_capabilities(self.root / "does-not-exist"), [])

    def test_capability_marker_also_matches_the_generated_response_type_name(self):
        """The marker is robust to upstream hoisting the literal into a constant."""
        by_type_name = """package app

var AcceptedQueries = []responder{
\tQueryGetCurrentEpochResponse{},
\tQueryListClaimRecipientsResponse{},
\tQueryEpochPerformanceSummaryByParticipantResponse{},
\tQueryTotalVestingAmountResponse{},
}
"""
        tree = write_gonka_tree(self.root / "typed", legacy_go=by_type_name)

        match = select_adapter(
            self.adapters, root=tree, commit_sha=UNLISTED_SHA, role="gonka"
        )

        self.assertEqual(match.adapter.adapter_id, ADAPTER_ID)


    def test_tree_without_the_upstream_gradle_wrapper_jar_is_rejected_by_the_immutable_adapter(self):
        # The mock server is built by invoking the wrapper jar directly; a tree
        # without it cannot be built without writing a wrapper into it.
        tree = write_gonka_tree(self.root / "no-wrapper", legacy_go=UPSTREAM_LEGACY_GO)
        (tree / "testermint/mock_server/gradle/wrapper/gradle-wrapper.jar").unlink()

        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            select_adapter(self.adapters, root=tree, commit_sha=UNLISTED_SHA, role="gonka")

        reasons = cm.exception.details["rejected"][ADAPTER_ID]
        self.assertTrue(any("gradle-wrapper.jar is missing" in reason for reason in reasons))


class ScenarioSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-compat-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.upstream = gonka_immutable_source_adapter()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_upstream_adapter_supports_its_native_and_boundary_scenarios(self):
        native = [
            "funded-claim",
            "network-unconfirmed",
            "claim-expiry-positive",
            "claim-expiry-zero",
            "terminal-release-repeat",
            "foreign-native-preservation",
            "late-donation-after-completed",
            "lock-exact-e",
            "lock-e-plus-4",
            "lock-e-plus-5",
            "refund-boundary-and-vesting-addition",
            "usdt-withdrawal-failure-recovery",
            "native-release-rollback-retry",
        ]
        boundary = [
            "go-query-error-classification",
            "wasm-abi-boundary",
            "contract-network-unconfirmed-policy",
            "contract-claim-expiry-policy",
            "contract-query-fault-policy",
        ]

        self.assertIsNone(
            assert_scenarios_supported(
                self.upstream, native + boundary, selection_label="native+boundary"
            )
        )
        self.assertIn("contract-query-fault-policy", self.upstream.supported_scenarios)
        self.assertNotIn("package-c-query-faults", self.upstream.supported_scenarios)
        self.assertEqual(dict(self.upstream.unsupported_scenarios), {})

    def test_scenario_unknown_to_the_adapter_is_refused_with_an_explicit_reason(self):
        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            assert_scenarios_supported(
                self.upstream, ["totally-new-scenario"], selection_label="custom"
            )

        self.assertEqual(
            cm.exception.details["unsupported"]["totally-new-scenario"],
            "not declared as supported by this compatibility adapter",
        )

    def test_an_adapter_without_declared_supported_scenarios_rejects_a_known_scenario(self):
        adapter = dataclasses.replace(self.upstream, supported_scenarios=frozenset())

        with self.assertRaises(UnsupportedCompatibilityError) as cm:
            assert_scenarios_supported(adapter, ["funded-claim"], selection_label="custom")

        self.assertIn("funded-claim", cm.exception.details["unsupported"])

    def test_empty_selection_is_accepted_without_raising(self):
        self.assertIsNone(
            assert_scenarios_supported(self.upstream, [], selection_label="empty")
        )


class ImmutableSourcePolicyTests(unittest.TestCase):
    """Nothing in an adapter can permit a changed source tree.

    These replace the old overlay-precondition tests: the overlay machinery
    (patch specs, the checksum manifest, production-overwrite flags) no longer
    exists, so the equivalent fact is that it cannot be declared at all.
    """

    RETIRED_FIELDS = ("patches", "allow_production_overwrite", "patch_source")

    def test_no_adapter_dataclass_field_can_declare_a_patch_or_a_production_overwrite(self):
        names = {field.name for field in dataclasses.fields(CompatibilityAdapter)}
        for retired in self.RETIRED_FIELDS:
            self.assertNotIn(retired, names)

    def test_every_adapter_declaration_and_lock_record_states_the_immutable_source_policy(self):
        for adapter in gonka_adapters() + contracts_adapters():
            with self.subTest(adapter=adapter.adapter_id):
                declaration = adapter.declaration()
                record = adapter.lock_record(match_mode="markers", measurements={})
                self.assertEqual(declaration["source_policy"], "immutable")
                self.assertEqual(record["source_policy"], "immutable")
                for retired in self.RETIRED_FIELDS:
                    self.assertNotIn(retired, declaration)
                    self.assertNotIn(retired, record)

    def test_the_overlay_machinery_is_not_importable_from_compat_any_more(self):
        for removed in (
            "apply_patches", "build_patch_specs", "read_overlay_manifest",
            "patch_manifest_document", "is_production_module", "OVERLAY_MANIFEST_NAME",
            "upstream_wasm_queries_adapter", "PatchSpec", "AppliedPatch",
        ):
            self.assertFalse(hasattr(compat, removed), removed)

    def test_the_gonka_runtime_expectation_is_the_selected_commit_itself(self):
        expectations = {e.field_name: e for e in gonka_immutable_source_adapter().runtime}

        self.assertIs(expectations["gonka_source_sha"].kind, ExpectationKind.SELECTED_COMMIT)
        self.assertFalse(hasattr(ExpectationKind, "PREPARED_COMMIT"))


class ImmutableGonkaRecipeTests(unittest.TestCase):
    """The recipe builds the selected commit and writes nothing into it."""

    def setUp(self):
        self.recipe = gonka_immutable_source_adapter().build
        self.steps = {step.step_id: step for step in self.recipe.steps}

    def test_recipe_reproduces_the_upstream_image_targets_in_order(self):
        self.assertEqual(self.recipe.recipe_id, "gonka-immutable-images-v2")
        self.assertEqual(
            [step.step_id for step in self.recipe.steps],
            [
                "stage-inferenced-context", "inferenced-image", "api-image",
                "edge-api-image", "proxy-image", "mock-server-jar",
                "stage-mock-server-context", "mock-server-image",
            ],
        )

    def test_no_step_runs_inside_a_snapshot(self):
        for step in self.recipe.steps:
            with self.subTest(step=step.step_id):
                self.assertEqual(step.cwd_role, "build_out")

    def test_every_staged_context_and_declared_output_lives_under_the_build_directory(self):
        for step in self.recipe.steps:
            for copy in step.stage:
                with self.subTest(step=step.step_id, destination=copy.destination):
                    self.assertTrue(copy.destination.startswith("{build_out}/"))
            for produced in step.produces_files:
                with self.subTest(step=step.step_id, produced=produced):
                    # produces_files are relative to {build_out}.
                    self.assertFalse(produced.startswith("{"))
                    self.assertFalse(produced.startswith("/"))
        self.assertEqual(
            self.steps["inferenced-image"].argv[-1], "{build_out}/" + INFERENCED_CONTEXT_RELPATH
        )
        self.assertEqual(
            self.steps["mock-server-image"].argv[-1], "{build_out}/" + MOCK_SERVER_CONTEXT_RELPATH
        )

    def test_mock_server_is_built_with_the_upstream_wrapper_jar_and_never_with_gradlew(self):
        step = self.steps["mock-server-jar"]
        argv = list(step.argv)

        self.assertEqual(argv[0], "java")
        self.assertEqual(
            argv[argv.index("-cp") + 1],
            "{gonka}/testermint/mock_server/gradle/wrapper/gradle-wrapper.jar",
        )
        self.assertIn("org.gradle.wrapper.GradleWrapperMain", argv)
        self.assertFalse(any("gradlew" in token for token in argv))
        self.assertTrue(argv[argv.index("--project-cache-dir") + 1].startswith("{build_out}/"))
        self.assertIn("-Pa8.outRoot={build_out}/mock-server-build", argv)
        self.assertTrue(step.env["GRADLE_USER_HOME"].startswith("{build_out}/"))
        self.assertEqual(step.produces_files, (MOCK_SERVER_JAR_RELPATH,))

    def test_the_commit_reaches_the_binaries_only_through_the_upstream_ldflags(self):
        ldflags = dict(self.steps["inferenced-image"].build_args)["LDFLAGS"]

        self.assertIn("version.Commit={gonka_sha}", ldflags)
        self.assertIn("version.Version={gonka_version}", ldflags)
        self.assertNotIn("prepared", " ".join(self.steps["inferenced-image"].argv))

    def test_every_chain_image_is_produced_by_exactly_one_step(self):
        produced = [image for step in self.recipe.steps for image in step.produces_images]

        self.assertEqual(sorted(produced), sorted(self.recipe.chain_images))
        self.assertEqual(len(produced), len(set(produced)))

    def test_changing_one_step_argument_changes_the_recipe_fingerprint(self):
        original = self.recipe.fingerprint()
        step = self.steps["proxy-image"]
        changed_step = dataclasses.replace(step, argv=step.argv[:-1] + ("{gonka}",))
        changed = dataclasses.replace(
            self.recipe,
            steps=tuple(changed_step if s.step_id == "proxy-image" else s for s in self.recipe.steps),
        )

        self.assertNotEqual(changed.fingerprint(), original)
        self.assertEqual(gonka_immutable_source_adapter().build.fingerprint(), original)


class BuildRecipeVersionAwarenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-compat-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.upstream = gonka_immutable_source_adapter()
        self.contracts = marketplace_contracts_adapter()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_contracts_recipe_builds_the_selected_contracts_sha(self):
        recipe = self.contracts.build
        release = next(s for s in recipe.steps if s.step_id == "a9-release")

        self.assertIn("{contracts_sha}", release.argv)
        self.assertIn("{python}", release.argv)
        self.assertIn("{runner}/vendor/contract_release/release.py", release.argv)
        self.assertEqual(release.argv[release.argv.index("--repo") + 1], "{contracts}")
        self.assertIn("{build_out}/a9-release", release.argv)
        self.assertEqual(release.cwd_role, "contracts")
        self.assertEqual(recipe.recipe_id, "contracts-a9-release-v1")

    def test_external_images_and_toolchain_are_pinned_by_digest(self):
        recipe = self.upstream.build

        self.assertIn(
            "@sha256:b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e",
            recipe.external_images["cosmwasm-optimizer"],
        )
        self.assertEqual(recipe.toolchain["rust"], "1.81.0")
        self.assertEqual(recipe.toolchain["go"], "1.26.8")
        self.assertEqual(recipe.toolchain["node"], "22.23.3")

    def test_runtime_expectations_are_measured_from_the_targets_own_go_mod(self):
        tree = write_gonka_tree(self.root / "measured", legacy_go=UPSTREAM_LEGACY_GO)
        expectations = {e.field_name: e for e in self.upstream.runtime}

        self.assertEqual(
            measure_go_module_version(tree, "inference-chain/go.mod", "github.com/CosmWasm/wasmd"),
            "v0.53.0",
        )
        self.assertEqual(
            expectations["wasmd"].expected_value(sources_root=tree, selected_sha=None),
            "v0.53.0",
        )
        self.assertEqual(
            expectations["wasmvm"].expected_value(sources_root=tree, selected_sha=None),
            "v2.1.2",
        )
        self.assertEqual(
            expectations["gonka_source_sha"].expected_value(
                sources_root=tree, selected_sha="feedface" * 5
            ),
            "feedface" * 5,
        )

    def test_comments_on_single_and_block_require_declarations_preserve_the_version(self):
        for declaration in (
            "require github.com/CosmWasm/wasmd v0.53.0",
            "require (\n\tgithub.com/CosmWasm/wasmd v0.53.0\n)",
        ):
            baseline = "module example\n\n" + declaration + "\n"
            for comment in ("", " // pinned"):
                with self.subTest(declaration=declaration, comment=comment):
                    tree = write_gonka_tree(
                        self.root / "commented-require",
                        go_mod=baseline.replace("v0.53.0", "v0.53.0" + comment),
                    )
                    self.assertEqual(
                        measure_go_module_version(
                            tree, "inference-chain/go.mod", "github.com/CosmWasm/wasmd"
                        ),
                        "v0.53.0",
                    )

    def test_an_excluded_version_does_not_count_as_a_required_dependency(self):
        tree = write_gonka_tree(
            self.root / "excluded-module", go_mod=GO_MOD.replace("require (", "exclude (")
        )
        self.assertIsNone(
            measure_go_module_version(
                tree, "inference-chain/go.mod", "github.com/CosmWasm/wasmd"
            )
        )

    def test_an_exclude_block_before_require_does_not_override_the_required_version(self):
        tree = write_gonka_tree(
            self.root / "excluded-and-required",
            go_mod=GO_MOD.replace(
                "require (",
                "exclude (\n\tgithub.com/CosmWasm/wasmd v0.52.0\n)\n\nrequire (",
            ),
        )
        self.assertEqual(
            measure_go_module_version(
                tree, "inference-chain/go.mod", "github.com/CosmWasm/wasmd"
            ),
            "v0.53.0",
        )

    def test_go_module_version_is_none_when_the_module_is_absent(self):
        tree = write_gonka_tree(
            self.root / "no-wasmvm",
            legacy_go=UPSTREAM_LEGACY_GO,
            go_mod="module x\n\ngo 1.23.6\n",
        )

        self.assertIsNone(
            measure_go_module_version(
                tree, "inference-chain/go.mod", "github.com/CosmWasm/wasmvm/v2"
            )
        )
        self.assertIsNone(
            measure_go_module_version(tree, "does/not/exist/go.mod", "github.com/CosmWasm/wasmd")
        )

    def test_adapter_lock_record_carries_the_match_mode_and_measurements(self):
        record = self.upstream.lock_record(
            match_mode="markers", measurements={"exposed_marketplace_queries": ["current_epoch"]}
        )

        self.assertEqual(record["adapter_id"], ADAPTER_ID)
        self.assertEqual(record["match_mode"], "markers")
        self.assertEqual(record["source_policy"], "immutable")
        self.assertEqual(record["build_recipe_fingerprint"], self.upstream.build.fingerprint())
        self.assertEqual(record["unsupported_scenarios"], {})
        self.assertEqual(record["adapter_content_hash"], self.upstream.content_hash())


class ExternalHarnessScenarioSourceTests(unittest.TestCase):
    def test_live_launcher_selects_the_exact_catalog_method_for_every_native_scenario(self):
        import ast
        from forward_e2e.suite.catalog import NATIVE_TASKS
        # Read the standalone launcher's actual dispatch table without executing
        # it. A stale selector otherwise reaches Gradle but executes zero tests.
        launcher = Path(__file__).resolve().parents[3] / "scripts" / "acceptance_harness.py"
        tree = ast.parse(launcher.read_text(encoding="utf-8"))
        mapping = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "LIVE_SCENARIO_TESTS"
        )
        expected = {
            task.scenario_selector: f"MarketplaceContractAcceptanceTests.{task.exact_test_method}"
            for task in NATIVE_TASKS
        }
        self.assertEqual(mapping, expected)


    def test_every_kotlin_acceptance_test_has_one_catalog_owner(self):
        # A whole-class JUnit invocation must not discover a leftover monolith
        # that the canonical runner never exercises.
        import re
        from forward_e2e.suite.catalog import NATIVE_TASKS
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        methods = re.findall(r"@Test\s+(?:@Timeout[^\n]*\n\s*)?fun `([^`]+)`", source)
        # This deliberately supports a narrow Kotlin declaration shape. Fail
        # explicitly on other annotations or formatting instead of silently
        # dropping a test from the catalog ownership check.
        declarations = re.findall(r"@(?:org\.junit\.jupiter\.api\.)?Test\b", source)
        self.assertEqual(
            len(methods), len(declarations),
            "Unrecognized @Test declaration: update discovery before checking catalog ownership",
        )
        expected = [task.exact_test_method for task in NATIVE_TASKS]
        self.assertCountEqual(methods, expected)


    def test_e_plus_2_wait_precedes_manual_claim_restart_resumed_settlement_and_release(self):
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        funded = source.split(
            "fun `marketplace funded claim settles and releases on real Gonka`()", 1
        )[1].split("\n    @Test", 1)[0]

        stopped = funded.index("participant.stopOwnedApiContainer()")
        wait_for_main_summary = funded.index(
            "genesis.waitForStage(EpochStage.CLAIM_REWARDS, offset = 2)", stopped
        )
        manual_claim = funded.index('            "--claim-only",', stopped)
        restart = funded.index("stoppedApi.startSameContainer()", manual_claim)
        resumed_settlement = funded.index('            "--resume-claim",', restart)
        first_release = funded.index('runHarness("release",', resumed_settlement)

        self.assertLess(stopped, manual_claim)
        self.assertLess(stopped, wait_for_main_summary)
        self.assertLess(wait_for_main_summary, manual_claim)
        self.assertLess(manual_claim, restart)
        self.assertLess(restart, resumed_settlement)
        self.assertLess(resumed_settlement, first_release)
        self.assertEqual(funded.count('runHarness("release",'), 1)
        self.assertEqual(funded.count('"release-scenario"'), 1)
        self.assertLess(first_release, funded.index('"release-scenario"'))
        self.assertIn('"--name", "bootstrap"', funded)
        for unrelated in (
            "prepareDeal(", "gas-sweep-scenario", "verify-factory-isolation",
            "network-unconfirmed", "no-buyer-expired", "no-sale",
        ):
            self.assertNotIn(unrelated, funded)

    def test_split_routing_deals_use_distinct_host_epoch_keys(self):
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        routing = source.split(
            "fun `marketplace funded routing refunds are isolated and atomic`()", 1
        )[1].split("\n    @Test", 1)[0]
        self.assertIn("val routingMissingEpoch = targetEpoch + 1", routing)
        self.assertIn('"routing-mismatch", targetEpoch, funded = true', routing)
        self.assertIn('"routing-missing", routingMissingEpoch, funded = true', routing)
        self.assertIn("routing-missing fixture missed its exact target epoch", routing)

    def test_no_buyer_claim_expiry_is_an_independent_exact_scenario(self):
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        scenario = source.split(
            "fun `marketplace no buyer claim expiry preserves buyer absence`()", 1
        )[1].split("\n    @Test", 1)[0]

        self.assertIn(
            'prepareDeal(\n            "no-buyer-expired", targetEpoch, funded = false',
            scenario,
        )
        self.assertIn("val unclaimedHost = cluster.joinPairs.first()", scenario)
        self.assertIn("unclaimedHost.stopOwnedApiContainer()", scenario)
        self.assertEqual(scenario.count('"--name", "no-buyer-expired"'), 5)
        self.assertEqual(scenario.count('"--require-positive"'), 2)
        self.assertIn('"--expect", "failure", "--reason", "too_early"', scenario)
        self.assertIn('"--expect", "success", "--reason", "claim_expiry"', scenario)
        self.assertLess(
            scenario.index('"--expect", "failure", "--reason", "too_early"'),
            scenario.index('"--expect", "success", "--reason", "claim_expiry"'),
        )

        from forward_e2e.suite.catalog import NATIVE_TASKS
        task = next(
            task for task in NATIVE_TASKS if task.task_id == "no-buyer-claim-expiry"
        )
        self.assertEqual(task.evidence_scopes, ["no-buyer-expired"])
        self.assertEqual(
            task.exact_test_method,
            "marketplace no buyer claim expiry preserves buyer absence",
        )
        self.assertNotIn("funded-extended", {task.task_id for task in NATIVE_TASKS})

    def test_no_sale_vesting_lifecycle_keeps_the_causal_asset_sequence(self):
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        scenario = source.split(
            "fun `marketplace no sale vesting lifecycle preserves every asset`()", 1
        )[1].split("\n    }", 1)[0]

        required = (
            '"verify-claimed-scenario"',
            '"lock-scenario"',
            '"before_settlement"',
            '"contaminate-scenario"',
            '"settle-scenario"',
            '"release-scenario"',
            '"assert-early-release-unavailable"',
            '"snapshot-vesting-scenario"',
            '"--require-non-empty"',
            '"verify-vesting-addition-scenario"',
            '"after_settlement"',
        )
        positions = [scenario.index(marker) for marker in required]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(scenario.count('"release-scenario"'), 3)
        self.assertIn('"no-sale", targetEpoch, funded = false', scenario)
        self.assertIn(
            'bootstrap(\n            targetEpoch,\n'
            '            hostNode = "genesis-node",\n'
            '            hostKey = "genesis",',
            scenario,
        )
        self.assertIn('hostNode = "join1-node", hostKey = "join1"', scenario)

        from forward_e2e.suite.catalog import NATIVE_TASKS
        task = next(
            task for task in NATIVE_TASKS
            if task.task_id == "no-sale-vesting-lifecycle"
        )
        self.assertEqual(task.evidence_scopes, ["no-sale"])
        self.assertIn("vesting_addition_verified", task.expected_checkpoints)
        self.assertIn("release_completed", task.expected_checkpoints)

    def test_emergency_host_only_recovery_keeps_the_causal_failure_and_retry_sequence(self):
        source = KOTLIN_ACCEPTANCE_SOURCE.read_text(encoding="utf-8")
        scenario = source.split(
            "fun `marketplace emergency refund host only release rolls back and retries`()", 1
        )[1].split("\n    @Test", 1)[0]
        helper_call = "refundAtEmergencyDeadline(genesis, cluster.joinPairs[1])"
        self.assertEqual(scenario.count(helper_call), 1)
        self.assertLess(scenario.index("initCluster("), scenario.index(helper_call))
        helper = source.split("private fun refundAtEmergencyDeadline(", 1)[1].split(
            "\n    }", 1
        )[0]
        # Check the executed sequence across the helper boundary so extracting
        # setup cannot hide a missing deadline assertion or reorder the refund.
        scenario = scenario.replace(helper_call, helper)

        required = (
            '"lock-scenario"',
            '"--expected-offset", "2"',
            '"--reason", "network_unconfirmed_too_early"',
            '"--expected-offset", "3"',
            '"--reason", "network_unconfirmed"',
            '"after_terminal_emergency_refund"',
            'UpdateRestrictionsParams(',
            '"bank-release-rollback-scenario"',
            'waitForMinimumBlock(restrictionEndBlock + 1',
            '"bank-release-retry-scenario"',
        )
        positions = [scenario.index(marker) for marker in required]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('"--expected-send-index", "1"', scenario)
        # The donated liquid GNK is the rollback fixture, not a zero-balance
        # precondition. A premature NothingToRelease probe would contradict it.
        self.assertNotIn('"assert-early-release-unavailable"', scenario)
        self.assertEqual(scenario.count('"--name", "network-unconfirmed"'), 8)

        from forward_e2e.suite.catalog import NATIVE_TASKS
        task = next(
            task for task in NATIVE_TASKS
            if task.task_id == "emergency-host-only-recovery"
        )
        self.assertEqual(task.evidence_scopes, ["network-unconfirmed"])
        self.assertIn("bank_rollback_atomic", task.expected_checkpoints)
        self.assertIn("bank_send_retry_succeeds", task.expected_checkpoints)


if __name__ == "__main__":
    unittest.main()
