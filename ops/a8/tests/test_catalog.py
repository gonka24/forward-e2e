"""Unit tests for A8 catalog profiles, legacy alias rejection, and catalog hashing.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ops.a8.catalog import (
    NATIVE_TASKS,
    compute_catalog_hash,
    get_profile_tasks,
    get_task_by_id_or_alias,
    resolve_e2e_selection,
)
from ops.a8.models import ProofLevel


class CatalogTests(unittest.TestCase):
    def test_native_profile_has_19_exact_selectors_in_fixed_order(self):
        tasks = get_profile_tasks("native")
        expected_order = [
            "funded-claim",
            "network-unconfirmed",
            "claim-expiry-positive",
            "claim-expiry-zero",
            "terminal-release-repeat",
            "b3-foreign-native",
            "late-donation-after-completed",
            "lock-exact-e",
            "lock-e-plus-4",
            "lock-e-plus-5",
            "package-a-r1-r2",
            "package-b-r6-1",
            "package-b-r7-1",
            "funded-routing-refunds",
            "unfunded-lock-boundaries",
            "funded-gas-sweep",
            "no-buyer-claim-expiry",
            "no-sale-vesting-lifecycle",
            "emergency-host-only-recovery",
        ]
        actual_order = [t.task_id for t in tasks]
        self.assertEqual(actual_order, expected_order)
        self.assertEqual([t.scenario_selector for t in tasks], expected_order)

        self.assertEqual([t.ordinal for t in tasks], list(range(1, 20)))

        for t in tasks:
            self.assertEqual(t.proof_level, ProofLevel.NATIVE)

    def test_funded_claim_exact_id_resolution_and_rejection_of_legacy_full_alias(self):
        t1 = get_task_by_id_or_alias("funded-claim")
        self.assertIsNotNone(t1)
        self.assertEqual(t1.task_id, "funded-claim")

        self.assertIsNone(get_task_by_id_or_alias("full"))
        with self.assertRaisesRegex(ValueError, "Unknown scenario: 'full'"):
            resolve_e2e_selection(scenarios=["full"])

    def test_smoke_profile_is_lock_exact_e_only(self):
        tasks = get_profile_tasks("smoke")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "lock-exact-e")
        self.assertEqual(tasks[0].ordinal, 1)

    def test_boundary_profile_has_5_exact_tasks(self):
        tasks = get_profile_tasks("boundary")
        expected = [
            ("go-boundary", ProofLevel.GO_BOUNDARY),
            ("wasm-abi-boundary", ProofLevel.WASM_ABI),
            ("ct-network-unconfirmed", ProofLevel.CONTRACT_TEST),
            ("ct-claim-expiry", ProofLevel.CONTRACT_TEST),
            ("ct-package-c-policy", ProofLevel.CONTRACT_TEST),
        ]
        self.assertEqual([(task.task_id, task.proof_level) for task in tasks], expected)

    def test_all_profile_orders_boundary_then_native_without_duplicates(self):
        tasks = get_profile_tasks("all")
        self.assertEqual(len(tasks), 24)  # 5 boundary + 19 native
        ids = [t.task_id for t in tasks]
        self.assertEqual(len(set(ids)), 24)

        self.assertEqual(
            ids[:5],
            [
                "go-boundary",
                "wasm-abi-boundary",
                "ct-network-unconfirmed",
                "ct-claim-expiry",
                "ct-package-c-policy",
            ],
        )
        self.assertEqual(ids[5:], [t.task_id for t in NATIVE_TASKS])

        self.assertEqual([t.ordinal for t in tasks], list(range(1, 25)))

    def test_unchanged_catalog_produces_the_same_sha256_hex_digest(self):
        h1 = compute_catalog_hash()
        h2 = compute_catalog_hash()
        self.assertEqual(h1, h2)
        self.assertRegex(h1, r"\A[0-9a-f]{64}\Z")

    def test_changing_a_native_or_boundary_task_timeout_changes_the_catalog_hash(self):
        baseline = compute_catalog_hash()
        for task_id in ("funded-claim", "go-boundary"):
            with self.subTest(task_id=task_id):
                task = get_task_by_id_or_alias(task_id)
                self.assertIsNotNone(task)
                # Change one fact on a real catalog entry: a lock must detect
                # a changed execution budget in either part of the catalog.
                with patch.object(
                    task, "stage_timeout_seconds", task.stage_timeout_seconds + 1
                ):
                    self.assertNotEqual(compute_catalog_hash(), baseline)
                self.assertEqual(compute_catalog_hash(), baseline)


if __name__ == "__main__":
    unittest.main()
