"""Unit tests for the single selection resolver in ops/a8/catalog.py.

``resolve_e2e_selection`` is the single place that resolves user-supplied
``--profile`` and ``--scenario`` selections into an ordered list of TaskPlan
objects following the canonical catalog order.

The companion guarantee lives in ops/a8/e2e/compat.py: a profile must never go
green by silently skipping a scenario the matched compatibility adapter cannot
run, so ``assert_scenarios_supported`` is exercised against the real adapters.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from ops.a8.catalog import (
    BOUNDARY_TASKS,
    NATIVE_TASKS,
    _CATALOG_ORDER,
    get_profile_tasks,
    resolve_e2e_selection,
)
from ops.a8.e2e.compat import (
    CompatibilityAdapter,
    assert_scenarios_supported,
    gonka_immutable_source_adapter,
    marketplace_contracts_adapter,
)
from ops.a8.e2e.errors import UnsupportedCompatibilityError


class E2ESelectionTests(unittest.TestCase):
    # -- normalisation ---------------------------------------------------
    def test_repeated_scenarios_are_deduplicated_and_ordered_by_the_catalog(self):
        tasks, profile, scenarios = resolve_e2e_selection(
            scenarios=[
                "lock-exact-e",
                "go-boundary",
                "lock-exact-e",
                "funded-claim",
                "go-boundary",
            ]
        )
        self.assertIsNone(profile)
        ids = [task.task_id for task in tasks]
        self.assertEqual(ids, ["go-boundary", "funded-claim", "lock-exact-e"])
        self.assertEqual(scenarios, ids)
        self.assertEqual([task.ordinal for task in tasks], [1, 2, 3])

    def test_command_line_order_never_changes_the_execution_order(self):
        forwards, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["go-boundary", "funded-claim"]
        )
        backwards, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["funded-claim", "go-boundary"]
        )
        self.assertEqual(
            [task.task_id for task in forwards], [task.task_id for task in backwards]
        )
        self.assertEqual([task.task_id for task in forwards], ["go-boundary", "funded-claim"])

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
        self.assertIn("ct-package-c-policy", scenario_ids)
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
            supported_scenarios=frozenset({"go-boundary", "funded-claim"}),
            unsupported_scenarios={"lock-exact-e": "declared unsupported on purpose"},
        )
        tasks, _profile, _scenarios = resolve_e2e_selection(
            scenarios=["go-boundary", "funded-claim", "lock-exact-e", "lock-e-plus-4"]
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


if __name__ == "__main__":
    unittest.main()
