"""Unit tests for the single selection resolver in forward_e2e/suite/catalog.py.

``resolve_e2e_selection`` is the single place that resolves user-supplied
``--profile`` and ``--scenario`` selections into an ordered list of TaskPlan
objects following the canonical catalog order.

The companion guarantee lives in forward_e2e/execution/compat.py: a profile must never go
green by silently skipping a scenario the matched compatibility adapter cannot
run, so ``assert_scenarios_supported`` is exercised against the real adapters.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from forward_e2e.suite.catalog import (
    BOUNDARY_TASKS,
    LEGACY_SCENARIO_ALIASES,
    NATIVE_TASKS,
    _CATALOG_ORDER,
    canonical_task_id,
    get_profile_tasks,
    get_task_by_id_or_alias,
    legacy_aliases_in,
    resolve_e2e_selection,
)
from forward_e2e.execution.compat import (
    CompatibilityAdapter,
    assert_scenarios_supported,
    gonka_immutable_source_adapter,
    marketplace_contracts_adapter,
)
from forward_e2e.execution.errors import UnsupportedCompatibilityError


class E2ESelectionTests(unittest.TestCase):
    # -- normalisation ---------------------------------------------------
    def test_repeated_scenarios_are_deduplicated_and_ordered_by_the_catalog(self):
        tasks, profile, scenarios = resolve_e2e_selection(
            scenarios=[
                "lock-exact-e",
                "go-boundary",
                "lock-exact-e",
                "funded-claim",
                "go-query-error-classification",
            ]
        )
        self.assertIsNone(profile)
        ids = [task.task_id for task in tasks]
        self.assertEqual(ids, ["go-query-error-classification", "funded-claim", "lock-exact-e"])
        self.assertEqual(scenarios, ids)
        self.assertEqual([task.ordinal for task in tasks], [1, 2, 3])

    def test_command_line_order_never_changes_the_execution_order(self):
        forwards, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["go-query-error-classification", "funded-claim"]
        )
        backwards, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["funded-claim", "go-boundary"]
        )
        self.assertEqual(
            [task.task_id for task in forwards], [task.task_id for task in backwards]
        )
        self.assertEqual(
            [task.task_id for task in forwards],
            ["go-query-error-classification", "funded-claim"],
        )

    def test_comma_separated_and_repeated_spellings_collapse_to_ordered_tasks(self):
        tasks, _profile, scenarios = resolve_e2e_selection(
            scenarios=["lock-exact-e,funded-claim", "funded-claim"]
        )
        self.assertEqual([task.task_id for task in tasks], ["funded-claim", "lock-exact-e"])
        self.assertEqual(scenarios, ["funded-claim", "lock-exact-e"])

    def test_catalog_order_is_boundary_tasks_then_native_tasks(self):
        self.assertEqual(
            _CATALOG_ORDER,
            [t.task_id for t in BOUNDARY_TASKS] + [t.task_id for t in NATIVE_TASKS],
        )
        self.assertEqual(len(set(_CATALOG_ORDER)), len(_CATALOG_ORDER))

    # -- rejections ------------------------------------------------------
    def test_unknown_scenario_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown scenario"):
            resolve_e2e_selection(scenarios=["no-such-scenario"])
        with self.assertRaisesRegex(ValueError, "Unknown scenario"):
            resolve_e2e_selection(scenarios=["lock-exact-e", "no-such-scenario"])

    def test_empty_selections_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Scenario name cannot be empty"):
            resolve_e2e_selection(scenarios=[""])
        with self.assertRaisesRegex(ValueError, "Scenario name cannot be empty"):
            resolve_e2e_selection(scenarios=["   "])
        with self.assertRaisesRegex(ValueError, "Scenario name cannot be empty"):
            resolve_e2e_selection(scenarios=[","])
        with self.assertRaisesRegex(
            ValueError, "Either --profile or --scenario must be specified"
        ):
            resolve_e2e_selection(profile=None, scenarios=None)
        with self.assertRaisesRegex(
            ValueError, "Either --profile or --scenario must be specified"
        ):
            resolve_e2e_selection(profile="   ", scenarios=[])

    def test_empty_component_inside_a_scenario_list_is_rejected(self):
        for spelling in ("go-boundary,", ",go-boundary", "go-boundary,,funded-claim"):
            with self.subTest(spelling=spelling), self.assertRaisesRegex(
                ValueError, "Scenario name cannot be empty"
            ):
                resolve_e2e_selection(scenarios=[spelling])

    def test_profile_and_scenario_cannot_be_combined(self):
        with self.assertRaisesRegex(
            ValueError, "Cannot combine --profile with --scenario"
        ):
            resolve_e2e_selection(profile="all", scenarios=["funded-claim"])

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown profile"):
            resolve_e2e_selection(profile="everything")

    def test_profile_selection_returns_the_catalog_profile_unchanged(self):
        tasks, profile, scenarios = resolve_e2e_selection(profile="ALL")
        self.assertEqual(profile, "all")
        self.assertIsNone(scenarios)
        self.assertEqual(
            [task.to_dict() for task in tasks],
            [task.to_dict() for task in get_profile_tasks("all")],
        )
        self.assertEqual(len(tasks), 24)

    # -- the replacement is part of all and works with upstream ------------
    def test_profile_all_contains_the_replacement_instead_of_the_removed_scenario(self):
        tasks, _profile, _scenarios = resolve_e2e_selection(profile="all")
        scenario_ids = [task.task_id for task in tasks]
        self.assertIn("contract-query-fault-policy", scenario_ids)
        self.assertNotIn("package-c-query-faults", scenario_ids)

    def test_profile_all_is_supported_by_the_upstream_adapter(self):
        tasks, profile, _scenarios = resolve_e2e_selection(profile="all")
        scenario_ids = [task.task_id for task in tasks]
        adapter = gonka_immutable_source_adapter()
        assert_scenarios_supported(adapter, scenario_ids, selection_label=f"--profile {profile}")

    def test_removed_package_c_selector_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown scenario"):
            resolve_e2e_selection(scenarios=["package-c-query-faults"])

    def test_profile_all_is_supported_by_the_marketplace_contracts_adapter(self):
        tasks, profile, _scenarios = resolve_e2e_selection(profile="all")
        scenario_ids = [task.task_id for task in tasks]
        assert_scenarios_supported(
            marketplace_contracts_adapter(),
            scenario_ids,
            selection_label=f"--profile {profile}",
        )

    def test_support_check_names_every_offender_of_a_synthetic_adapter(self):
        adapter = CompatibilityAdapter(
            adapter_id="synthetic-adapter-v1",
            role="gonka",
            description="Synthetic adapter used to pin the enforcement contract.",
            verified_commits=frozenset(),
            markers=(),
            supported_scenarios=frozenset({"go-query-error-classification", "funded-claim"}),
            unsupported_scenarios={"lock-exact-e": "declared unsupported on purpose"},
        )
        tasks, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["go-query-error-classification", "funded-claim", "lock-exact-e", "lock-e-plus-4"]
        )
        with self.assertRaises(UnsupportedCompatibilityError) as caught:
            assert_scenarios_supported(
                adapter,
                [task.task_id for task in tasks],
                selection_label="--scenario selection",
            )
        offenders = caught.exception.details["unsupported"]
        self.assertEqual(
            sorted(offenders), ["lock-e-plus-4", "lock-exact-e"]
        )
        self.assertEqual(offenders["lock-exact-e"], "declared unsupported on purpose")
        self.assertEqual(
            offenders["lock-e-plus-4"],
            "not declared as supported by this compatibility adapter",
        )
        self.assertEqual(caught.exception.exit_code, 1)


class LegacyScenarioAliasTests(unittest.TestCase):
    """Pre-rename scenario IDs are accepted as input and nowhere else."""

    def test_every_alias_maps_to_a_canonical_id_that_is_itself_not_an_alias(self):
        self.assertTrue(LEGACY_SCENARIO_ALIASES)
        for alias, canonical in LEGACY_SCENARIO_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertNotEqual(alias, canonical)
                self.assertIn(canonical, _CATALOG_ORDER)
                self.assertNotIn(alias, _CATALOG_ORDER)
                self.assertEqual(get_task_by_id_or_alias(alias).task_id, canonical)

    def test_an_alias_and_its_canonical_id_in_one_request_select_the_task_once_with_identical_budgets(self):
        for alias, canonical in LEGACY_SCENARIO_ALIASES.items():
            with self.subTest(alias=alias):
                mixed, _profile, normalised = resolve_e2e_selection(scenarios=[alias, canonical, alias])
                canonical_only, _profile, _ = resolve_e2e_selection(scenarios=[canonical])
                self.assertEqual(len(mixed), 1)
                self.assertEqual(normalised, [canonical])
                # The whole frozen task -- proof level, timeouts, Gradle budget,
                # checkpoints, artifacts, exact test method -- must be the same
                # object the canonical spelling selects.
                self.assertEqual(mixed[0].to_dict(), canonical_only[0].to_dict())
                self.assertEqual(mixed[0].task_id, canonical)
                self.assertEqual(mixed[0].ordinal, 1)

    def test_the_normalised_scenario_list_never_contains_an_alias(self):
        tasks, _profile, normalised = resolve_e2e_selection(
            scenarios=list(LEGACY_SCENARIO_ALIASES)
        )
        self.assertEqual(normalised, [task.task_id for task in tasks])
        self.assertEqual(set(normalised) & set(LEGACY_SCENARIO_ALIASES), set())
        self.assertEqual(set(normalised), set(LEGACY_SCENARIO_ALIASES.values()))

    def test_canonical_task_id_collapses_aliases_and_leaves_canonical_and_unknown_names_alone(self):
        for alias, canonical in LEGACY_SCENARIO_ALIASES.items():
            self.assertEqual(canonical_task_id(alias), canonical)
            self.assertEqual(canonical_task_id(canonical), canonical)
        self.assertEqual(canonical_task_id("not-a-task"), "not-a-task")

    def test_legacy_aliases_in_reports_each_alias_once_in_request_order_and_ignores_canonical_names(self):
        self.assertEqual(legacy_aliases_in(None), [])
        self.assertEqual(legacy_aliases_in(["lock-exact-e", "funded-claim,go-query-error-classification"]), [])
        self.assertEqual(
            legacy_aliases_in(["package-b-r7-1, go-boundary", "package-b-r7-1", "lock-exact-e"]),
            [
                ("package-b-r7-1", "native-release-rollback-retry"),
                ("go-boundary", "go-query-error-classification"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
