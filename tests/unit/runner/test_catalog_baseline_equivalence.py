"""The current catalog is the baseline catalog with renamed identifiers and nothing else.

The refactoring plan allows scenario IDs, Kotlin test method names and
descriptions to change, and forbids any change to what a task *is*: its
position, proof level, time budgets, mandatory checkpoints, evidence scopes,
coverage claims, limitations and mandatory artifacts. This module compares the
live catalog against ``tests/fixtures/compatibility/catalog-baseline-8079c0c.json``,
a frozen dump of those fields taken from the pre-refactoring commit. The
fixture carries its own rename maps, recorded when it was frozen, so the
comparison does not depend on the alias table of the code under test: removing
an alias on schedule leaves this test intact, while re-targeting one or
quietly changing a budget does not.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from forward_e2e.suite.catalog import (
    BOUNDARY_TASKS,
    NATIVE_TASKS,
    _CATALOG_ORDER,
    get_profile_tasks,
    get_task_by_id,
)

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/fixtures/compatibility/catalog-baseline-8079c0c.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class BaselineCatalogEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = _load_fixture()
        cls.renames = dict(cls.baseline["scenario_renames"])
        cls.method_renames = dict(cls.baseline["test_method_renames"])

    def canonical(self, old_id: str) -> str:
        return self.renames.get(old_id, old_id)

    def test_the_fixture_is_a_frozen_record_of_the_named_baseline_commit(self):
        self.assertEqual(self.baseline["baseline_commit"], "8079c0c274f5252800ee16b9f4fb0148bfcae2fe")
        self.assertIn("Never regenerate", self.baseline["_source"])
        self.assertEqual(len(self.baseline["tasks"]), 24)

    def test_the_catalog_still_has_exactly_the_baseline_tasks_in_the_baseline_order(self):
        mapped_order = [self.canonical(old) for old in self.baseline["catalog_order"]]
        self.assertEqual(mapped_order, list(_CATALOG_ORDER))
        self.assertEqual(len(BOUNDARY_TASKS), 5)
        self.assertEqual(len(NATIVE_TASKS), 19)

    def test_every_profile_selects_the_same_tasks_as_at_the_baseline(self):
        for profile, old_ids in self.baseline["profiles"].items():
            with self.subTest(profile=profile):
                self.assertEqual(
                    [self.canonical(old) for old in old_ids],
                    [task.task_id for task in get_profile_tasks(profile)],
                )

    def test_every_task_keeps_its_baseline_budgets_checkpoints_scopes_and_artifacts(self):
        for old in self.baseline["tasks"]:
            new_id = self.canonical(old["task_id"])
            with self.subTest(task=old["task_id"]):
                task = get_task_by_id(new_id)
                self.assertIsNotNone(task, f"baseline task {old['task_id']!r} has no counterpart {new_id!r}")
                self.assertEqual(task.task_id, new_id)
                current = task.to_dict()
                for field in self.baseline["frozen_fields"]:
                    expected = old[field]
                    if field == "expected_artifacts":
                        # Cargo-test logs are named after the task ID, so the
                        # rename is the only permitted difference.
                        expected = [
                            f"{new_id}.log" if item == f"{old['task_id']}.log" else item
                            for item in expected
                        ]
                    elif field == "exact_test_method":
                        expected = self.method_renames.get(expected, expected)
                    self.assertEqual(current[field], expected, f"{field} drifted for {new_id}")


if __name__ == "__main__":
    unittest.main()
