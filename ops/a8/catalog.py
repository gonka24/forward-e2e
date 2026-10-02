"""Versioned task catalog and profile definitions for A8 acceptance runner.

Zero external dependencies; uses strictly the Python standard library.
Does NOT import Linux-only fcntl or execute subprocesses on import.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence, Tuple

from .models import TOP_LEVEL_SCOPE, ProofLevel, TaskPlan


CATALOG_SCHEMA_VERSION = "1.0.0"

# Native tasks in fixed order with exact Kotlin test method names and separated stage budgets
NATIVE_TASKS: List[TaskPlan] = [
    TaskPlan(
        task_id="funded-claim",
        ordinal=1,
        proof_level=ProofLevel.NATIVE,
        description="Funded claim settles and releases on real Gonka",
        scenario_selector="funded-claim",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=100,  # 30m A9 + 10m test wasm + 60m Gradle
        stage_timeout_seconds=100 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:lock",
            "phase:claim_prepared",
            "phase:usdt_fault_rollback",
            "phase:claim_settle",
            "phase:release",
            "state_transition:Funded->Locked",
            "recipient_locked=true",
            "single_settlement_succeeds",
            "funded_claim_path_verified",
            "release_completed",
            "funded_release_lifecycle_verified",
        ],
        coverage_ids=["A1", "A2", "A3"],
        limitations=[
            "Single funded happy-path test; does not cover negative paths, no-sale, or gas sweep",
        ],
        exact_test_method="marketplace funded claim settles and releases on real Gonka",
        gradle_timeout_minutes=60,  # 55m JUnit + compilation/startup headroom
    ),
    TaskPlan(
        task_id="network-unconfirmed",
        ordinal=2,
        proof_level=ProofLevel.NATIVE,
        description="Absent native summary refunds only at emergency deadline",
        scenario_selector="network-unconfirmed",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["network-unconfirmed"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_summary_absent",
            "phase:refund_rejected",
            "phase:refund_committed",
            "emergency_deadline_reached",
            "emergency_refund_rejected_at_e_plus_2",
            "emergency_refund_committed",
        ],
        coverage_ids=["E1 missing summary"],
        limitations=[
            "Requires coincident Buyer/Host addresses for emergency sweep",
        ],
        exact_test_method="marketplace absent native summary refunds only at emergency deadline",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="claim-expiry-positive",
        ordinal=3,
        proof_level=ProofLevel.NATIVE,
        description="Positive unclaimed summary refunds only at claim expiry",
        scenario_selector="claim-expiry-positive",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["claim-expiry-positive"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_unclaimed_precondition",
            "phase:refund_rejected",
            "phase:refund_committed",
            "positive_unclaimed_precondition",
            "early_refund_rejected_at_e_plus_1",
            "claim_expiry_refund_committed_at_e_plus_2",
        ],
        coverage_ids=["D1 positive"],
        limitations=[
            "Validates positive unclaimed state at claim expiry",
        ],
        exact_test_method="marketplace positive unclaimed summary refunds only at claim expiry",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="claim-expiry-zero",
        ordinal=4,
        proof_level=ProofLevel.NATIVE,
        description="Zero unclaimed summary refunds only at claim expiry",
        scenario_selector="claim-expiry-zero",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["claim-expiry-zero"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_unclaimed_precondition",
            "phase:refund_rejected",
            "phase:refund_committed",
            "zero_unclaimed_genesis",
            "early_refund_rejected_at_e_plus_1",
            "claim_expiry_refund_committed_at_e_plus_2",
        ],
        coverage_ids=["D1 zero"],
        limitations=[
            "Requires special genesis configuration; not a production-config reachability claim",
        ],
        exact_test_method="marketplace zero unclaimed summary refunds only at claim expiry",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="terminal-release-repeat",
        ordinal=5,
        proof_level=ProofLevel.NATIVE,
        description="Terminal release repeat is rejected without additional payout",
        scenario_selector="terminal-release-repeat",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:release",
            "phase:terminal_release_repeat",
            "terminal_rejection_nothing_to_release",
            "state_entitlements_unchanged",
            "no_deal_bank_transfer",
        ],
        coverage_ids=["G3"],
        limitations=[
            "Asserts an included NothingToRelease rejection and unchanged state and balances",
        ],
        exact_test_method="marketplace terminal release repeat is rejected without payout",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="b3-foreign-native",
        ordinal=6,
        proof_level=ProofLevel.NATIVE,
        description="Successful release preserves foreign native denom",
        scenario_selector="b3-foreign-native",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:b3_foreign_native_successful_release",
            "foreign_native_funding_verified",
            "foreign_native_balance_preserved",
            "release_completed",
        ],
        coverage_ids=["B3"],
        limitations=[
            "Preserves foreign native denom alongside GNK",
        ],
        exact_test_method="marketplace successful release preserves foreign native denom",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="late-donation-after-completed",
        ordinal=7,
        proof_level=ProofLevel.NATIVE,
        description="Late liquid donations after Completed use cumulative GNK rounding",
        scenario_selector="late-donation-after-completed",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:release",
            "phase:late_completed_donation_release",
            "two_liquid_donations_verified",
            "cumulative_rounding_asserted",
        ],
        coverage_ids=["B2 liquid"],
        limitations=[
            "Proves liquid donation rounding math on completed deals",
        ],
        exact_test_method="marketplace late liquid donations after Completed use cumulative GNK rounding",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="lock-exact-e",
        ordinal=8,
        proof_level=ProofLevel.NATIVE,
        description="Funded lock succeeds exactly at E (lower boundary)",
        scenario_selector="lock-exact-e",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["lock-exact-e"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:lock_exact_e",
            "state_transition:Funded->Locked",
            "recipient_locked=true",
            "no_deal_bank_transfer",
            "lock_tx_included_in_epoch_e",
            "target_epoch_e_reached",
        ],
        coverage_ids=["C1 exact E lower boundary"],
        limitations=[
            "Waits for epoch E (up to 600s). Live network run, not a unit test.",
        ],
        exact_test_method="marketplace funded lock succeeds exactly at E",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="lock-e-plus-4",
        ordinal=9,
        proof_level=ProofLevel.NATIVE,
        description="Funded lock succeeds exactly at E plus 4",
        scenario_selector="lock-e-plus-4",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["lock-e-plus-4"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:lock_e_plus_4",
            "state_transition:Funded->Locked",
            "recipient_locked=true",
            "no_deal_bank_transfer",
            "lock_tx_included_in_epoch_e_plus_4",
            "target_epoch_e_plus_4_reached",
        ],
        coverage_ids=["C1 E+4 Lock"],
        limitations=[
            "Upper valid lock window boundary",
        ],
        exact_test_method="marketplace funded lock succeeds exactly at E plus 4",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="lock-e-plus-5",
        ordinal=10,
        proof_level=ProofLevel.NATIVE,
        description="Funded lock rejects exactly at E plus 5",
        scenario_selector="lock-e-plus-5",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["lock-e-plus-5"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:lock_e_plus_5_rejected",
            "lock_window_closed",
            "deal_remains_funded",
            "target_epoch_e_plus_5_reached",
        ],
        coverage_ids=["C1 E+5 Lock/pruning"],
        limitations=[
            "First expired lock window block; asserts exact error",
        ],
        exact_test_method="marketplace funded lock rejects exactly at E plus 5",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="package-a-r1-r2",
        ordinal=11,
        proof_level=ProofLevel.NATIVE,
        description="Package A preserves R1 refund boundary and releases a new vested gift",
        scenario_selector="package-a-r1-r2",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=["r1-refund-e-plus-5", "r2-vested-gift"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:r1_1_refund_e_plus_5_rejected",
            "phase:vesting_addition",
            "phase:r2_gift_checkpoint",
            "r1_refund_boundary_asserted",
            "vesting_addition_verified",
            "r2_gift_fully_locked",
            "r2_gift_first_unlocked",
            "r2_gift_payout_receipts_verified",
            "r2_gift_final_released",
        ],
        coverage_ids=["B2 vesting / R2"],
        limitations=[
            "Covers R1 refund boundary and R2 streamvesting gift mechanics",
        ],
        exact_test_method="marketplace package A preserves R1 refund boundary and releases a new vested gift",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="package-b-r6-1",
        ordinal=12,
        proof_level=ProofLevel.NATIVE,
        description="R6.1 rejects all three selected CW20 sends then settles once",
        scenario_selector="package-b-r6-1",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:claim_settle",
            "cw20_three_send_rejections_asserted",
            "settlement_atomic_rollback_verified",
            "single_settlement_succeeds",
        ],
        coverage_ids=["G1 / R6.1"],
        limitations=[
            "Injected CW20 transfer failures across Host/fee/Buyer send positions",
        ],
        exact_test_method="marketplace R6 dot 1 rejects all three selected CW20 sends then settles once",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="package-b-r7-1",
        ordinal=13,
        proof_level=ProofLevel.NATIVE,
        description="R7.1 rejects selected second Bank send then retries once",
        scenario_selector="package-b-r7-1",
        # Exact producer scopes owned by this Kotlin test.
        evidence_scopes=[TOP_LEVEL_SCOPE],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_bank_release_fault_plan",
            "phase:native_bank_release_rollback",
            "phase:native_bank_release_retry",
            "bank_send_rejection_asserted",
            "bank_send_retry_succeeds",
            "bank_rollback_atomic",
            "r7_1_two_bank_sends_verified",
        ],
        coverage_ids=["G2 / R7.1"],
        limitations=[
            "Bank send rollback and retry under native governance restriction",
        ],
        exact_test_method="marketplace R7 dot 1 rejects selected second Bank send then retries once",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="funded-routing-refunds",
        ordinal=14,
        proof_level=ProofLevel.NATIVE,
        description="Funded routing mismatch and missing refunds are atomic and Factory-isolated",
        scenario_selector="funded-routing-refunds",
        evidence_scopes=[TOP_LEVEL_SCOPE, "routing-mismatch", "routing-missing"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:routing_mutation",
            "phase:refund_committed",
            "phase:factory_isolation",
            "factory_isolation_verified",
            "routing_refund_committed",
            "routing_refund_fault_rollback",
            "both_routing_refunds_verified",
        ],
        coverage_ids=["routing missing/mismatch", "Factory isolation"],
        limitations=["Exact native routing rows on two funded Deals in one Factory"],
        exact_test_method="marketplace funded routing refunds are isolated and atomic",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="unfunded-lock-boundaries",
        ordinal=15,
        proof_level=ProofLevel.NATIVE,
        description="Unfunded Deals preserve Buyer absence at exact Lock E+4/E+5 boundaries",
        scenario_selector="unfunded-lock-boundaries",
        evidence_scopes=["lock-e-plus-4", "lock-e-plus-5"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=["live-context.json", "junit/TEST-MarketplaceContractAcceptanceTests.xml", "testermint.log"],
        expected_checkpoints=[
            "phase:lock",
            "phase:lock_rejected",
            "unfunded_lock_boundaries_verified",
        ],
        coverage_ids=["unfunded Lock E+4/E+5"],
        limitations=["Buyer-absent Open Deal boundary variant"],
        exact_test_method="marketplace unfunded lock boundaries preserve buyer absence",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="funded-gas-sweep",
        ordinal=16,
        proof_level=ProofLevel.NATIVE,
        description="Claimed positive reward refund gas sweep on a pristine Locked Deal",
        scenario_selector="funded-gas-sweep",
        evidence_scopes=["gas-claimed"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=["live-context.json", "junit/TEST-MarketplaceContractAcceptanceTests.xml", "testermint.log"],
        expected_checkpoints=[
            "phase:native_auto_claim",
            "phase:lock",
            "phase:claimed_refund_gas_sweep",
            "claimed_refund_gas_sweep_verified",
        ],
        coverage_ids=["claimed refund gas sweep"],
        limitations=["Direct and caller/submessage gas behavior after authoritative claimed=true"],
        exact_test_method="marketplace claimed refund gas sweep is isolated",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="no-buyer-claim-expiry",
        ordinal=17,
        proof_level=ProofLevel.NATIVE,
        description="No-Buyer positive unclaimed summary expires without inventing a Buyer payout",
        scenario_selector="no-buyer-claim-expiry",
        evidence_scopes=["no-buyer-expired"],
        timeout_minutes=85,
        stage_timeout_seconds=85 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_unclaimed_precondition",
            "phase:refund_rejected",
            "phase:refund_committed",
            "early_refund_rejected_at_e_plus_1",
            "claim_expiry_refund_committed_at_e_plus_2",
        ],
        coverage_ids=["no-Buyer claim expiry"],
        limitations=["Buyer is explicitly absent; positive summary remains claimed=false"],
        exact_test_method="marketplace no buyer claim expiry preserves buyer absence",
        gradle_timeout_minutes=45,
    ),
    TaskPlan(
        task_id="no-sale-vesting-lifecycle",
        ordinal=18,
        proof_level=ProofLevel.NATIVE,
        description="No-sale native reward preserves donations, foreign CW20 and old/new vesting",
        scenario_selector="no-sale-vesting-lifecycle",
        evidence_scopes=["no-sale"],
        timeout_minutes=100,
        stage_timeout_seconds=100 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:native_auto_claim",
            "phase:lock",
            "phase:liquid_donation",
            "phase:foreign_cw20_contamination",
            "phase:settle_claim",
            "phase:early_release_rejected",
            "phase:vesting_snapshot",
            "vesting_addition_verified",
            "release_completed",
            "early_release_unavailable_verified",
            "no_sale_vesting_lifecycle_verified",
        ],
        coverage_ids=["no-sale donations / foreign CW20 / non-empty vesting addition"],
        limitations=["Two Release attempts cover all tranches and the terminal no-op/rejection"],
        exact_test_method="marketplace no sale vesting lifecycle preserves every asset",
        gradle_timeout_minutes=60,
    ),
    TaskPlan(
        task_id="emergency-host-only-recovery",
        ordinal=19,
        proof_level=ProofLevel.NATIVE,
        description="Emergency refund terminal Deal releases a later donation to Host atomically",
        scenario_selector="emergency-host-only-recovery",
        evidence_scopes=["network-unconfirmed"],
        timeout_minutes=100,
        stage_timeout_seconds=100 * 60,
        expected_artifacts=[
            "live-context.json",
            "junit/TEST-MarketplaceContractAcceptanceTests.xml",
            "testermint.log",
        ],
        expected_checkpoints=[
            "phase:lock",
            "phase:native_summary_absent",
            "phase:refund_rejected",
            "phase:refund_committed",
            "phase:liquid_donation",
            "phase:native_bank_release_rollback",
            "phase:native_bank_release_retry",
            "emergency_refund_rejected_at_e_plus_2",
            "emergency_refund_committed",
            "bank_send_rejection_asserted",
            "bank_rollback_atomic",
            "bank_send_retry_succeeds",
        ],
        coverage_ids=["emergency refund HostOnly Bank rollback/retry"],
        limitations=["Single Host Bank send after terminal emergency refund"],
        exact_test_method="marketplace emergency refund host only release rolls back and retries",
        gradle_timeout_minutes=60,
    ),
]

# 5 Boundary tasks
BOUNDARY_TASKS: List[TaskPlan] = [
    TaskPlan(
        task_id="go-boundary",
        ordinal=1,
        proof_level=ProofLevel.GO_BOUNDARY,
        description="Go classification and JSON roundtrip tests in pinned Docker container",
        scenario_selector="go-boundary",
        timeout_minutes=30,
        stage_timeout_seconds=1800,
        expected_artifacts=[
            "build.log",
            "raw/go-test.json",
            "raw/exit-code",
            "report.json",
        ],
        expected_checkpoints=[
            "docker_build_completed",
            "TestToQuerierResultClassifiesVMSystemErrors_passed",
            "TestStrictPlanValidation_passed",
            "go_exit_zero",
        ],
        coverage_ids=["R4.4 InvalidResponse", "R4.5 InvalidRequest / Unknown / NoSuchContract / NoSuchCode"],
        limitations=[
            "Proves classification/serialization only; not libwasmvm FFI or contract error handling",
        ],
    ),
    TaskPlan(
        task_id="wasm-abi-boundary",
        ordinal=2,
        proof_level=ProofLevel.WASM_ABI,
        description="9-case CosmWasm ExternalQuerier ABI query_chain probe with synthetic Node host",
        scenario_selector="wasm-abi-boundary",
        timeout_minutes=15,
        stage_timeout_seconds=900,
        expected_artifacts=[
            "abi.json",
            "a8_query_boundary.wasm",
        ],
        expected_checkpoints=[
            "wasm_compiled",
            "9_abi_cases_passed",
            "status_pass",
        ],
        coverage_ids=["R4.4 InvalidResponse", "R4.5 InvalidRequest / Unknown / NoSuchContract / NoSuchCode"],
        limitations=[
            "Synthetic host; proves Rust query_chain ABI decoder, not production Go dispatcher/keeper reachability",
        ],
    ),
    TaskPlan(
        task_id="ct-network-unconfirmed",
        ordinal=3,
        proof_level=ProofLevel.CONTRACT_TEST,
        description="CT: network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
        scenario_selector="ct-network-unconfirmed",
        timeout_minutes=10,
        stage_timeout_seconds=600,
        expected_artifacts=[
            "ct-network-unconfirmed.log",
        ],
        expected_checkpoints=[
            "cargo_test_exit_zero",
            "test_network_unconfirmed_refund_passed",
            "non_zero_tests_executed",
        ],
        coverage_ids=["R4.6 encoding/overflow/accounting"],
        limitations=[
            "Contract decision policy unit test, not live fault injection",
        ],
    ),
    TaskPlan(
        task_id="ct-claim-expiry",
        ordinal=4,
        proof_level=ProofLevel.CONTRACT_TEST,
        description="CT: claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
        scenario_selector="ct-claim-expiry",
        timeout_minutes=10,
        stage_timeout_seconds=600,
        expected_artifacts=[
            "ct-claim-expiry.log",
        ],
        expected_checkpoints=[
            "cargo_test_exit_zero",
            "test_claim_expiry_checks_passed",
            "non_zero_tests_executed",
        ],
        coverage_ids=["R4.6 encoding/overflow/accounting"],
        limitations=[
            "Contract decision policy unit test, not live fault injection",
        ],
    ),
    TaskPlan(
        task_id="ct-package-c-policy",
        ordinal=5,
        proof_level=ProofLevel.CONTRACT_TEST,
        description="Contract-policy and cw-multi-test replacement for 71 Package C cases",
        scenario_selector="ct-package-c-policy",
        timeout_minutes=15,
        stage_timeout_seconds=900,
        expected_artifacts=["ct-package-c-policy.log"],
        expected_checkpoints=[
            "cargo_test_exit_zero",
            "18_exact_tests_passed",
            "71_case_markers_passed",
        ],
        coverage_ids=[
            "C2 / R3",
            "E2 policy / R4.1-4.3",
            "E3 policy / R5",
            "G1 / R6.3",
            "R4.4 UnsupportedRequest",
        ],
        limitations=[
            "Synthetic query boundaries; not production Gonka query-fault reachability or native E2E",
            "Modeled claim observations do not prove native claim/vesting ledger writes",
            "R7.2 remains covered by emergency-host-only-recovery at native level",
        ],
    ),
]

# Lookup maps
_NATIVE_BY_ID: Dict[str, TaskPlan] = {t.task_id: t for t in NATIVE_TASKS}
_BOUNDARY_BY_ID: Dict[str, TaskPlan] = {t.task_id: t for t in BOUNDARY_TASKS}
_ALL_TASKS: Dict[str, TaskPlan] = {**_NATIVE_BY_ID, **_BOUNDARY_BY_ID}


def get_task_by_id_or_alias(identifier: str) -> Optional[TaskPlan]:
    return _ALL_TASKS.get(identifier)


def get_profile_tasks(profile: str) -> List[TaskPlan]:
    norm = profile.strip().lower()
    if norm == "smoke":
        # Smoke = lock-exact-e only
        task = _NATIVE_BY_ID["lock-exact-e"]
        t = TaskPlan.from_dict(task.to_dict())
        t.ordinal = 1
        return [t]
    elif norm == "native":
        return [TaskPlan.from_dict(t.to_dict()) for t in NATIVE_TASKS]
    elif norm == "boundary":
        return [TaskPlan.from_dict(t.to_dict()) for t in BOUNDARY_TASKS]
    elif norm == "all":
        result: List[TaskPlan] = []
        ordinal = 1
        for b in BOUNDARY_TASKS:
            t = TaskPlan.from_dict(b.to_dict())
            t.ordinal = ordinal
            result.append(t)
            ordinal += 1
        for n in NATIVE_TASKS:
            t = TaskPlan.from_dict(n.to_dict())
            t.ordinal = ordinal
            result.append(t)
            ordinal += 1
        return result
    else:
        raise ValueError(f"Unknown profile: {profile!r}. Available: smoke, native, boundary, all")


#: Execution order of every registered task, taken from the catalog itself.
#: ``resolve_e2e_selection`` uses it so that the order of ``--scenario`` flags on
#: the command line can never change what runs first.
_CATALOG_ORDER: List[str] = [t.task_id for t in BOUNDARY_TASKS] + [t.task_id for t in NATIVE_TASKS]


def resolve_e2e_selection(
    profile: Optional[str] = None,
    scenarios: Optional[Sequence[str]] = None,
) -> Tuple[List[TaskPlan], Optional[str], Optional[List[str]]]:
    """Resolve a user selection into an ordered list of TaskPlan objects.

    Enforces the E2E command contract:
    * a repeated scenario is *normalised* (de-duplicated) instead of rejected,
      because ``--scenario x --scenario x`` expresses the same intent as
      ``--scenario x``;
    * the execution order comes from the catalog (``_CATALOG_ORDER``), not from
      the order in which the user happened to type the flags;
    * unknown and empty values are hard errors, and profile/scenario are
      mutually exclusive with exactly one of them required.
    """
    has_profile = bool(profile and profile.strip())
    has_scenarios = bool(scenarios and len(scenarios) > 0)

    if has_profile and has_scenarios:
        raise ValueError(
            "Cannot combine --profile with --scenario. A run selects tasks in exactly one way."
        )
    if not has_profile and not has_scenarios:
        raise ValueError("Either --profile or --scenario must be specified.")

    if has_profile:
        prof_name = str(profile).strip().lower()
        return get_profile_tasks(prof_name), prof_name, None

    assert scenarios is not None
    requested: List[str] = []
    for raw in scenarios:
        if raw is None or not str(raw).strip():
            raise ValueError("Scenario name cannot be empty")
        for part in str(raw).split(","):
            name = part.strip()
            if not name:
                raise ValueError("Scenario name cannot be empty")
            requested.append(name)

    if not requested:
        raise ValueError("No valid scenarios specified.")

    selected: set[str] = set()
    for name in requested:
        task = get_task_by_id_or_alias(name)
        if task is None:
            raise ValueError(
                f"Unknown scenario: {name!r}. Registered scenarios: {sorted(_ALL_TASKS.keys())}"
            )
        selected.add(task.task_id)

    resolved_tasks: List[TaskPlan] = []
    ordinal = 1
    for task_id in _CATALOG_ORDER:
        if task_id not in selected:
            continue
        t_copy = TaskPlan.from_dict(_ALL_TASKS[task_id].to_dict())
        t_copy.ordinal = ordinal
        resolved_tasks.append(t_copy)
        ordinal += 1

    return resolved_tasks, None, [t.task_id for t in resolved_tasks]


def compute_catalog_hash() -> str:
    data = {
        "version": CATALOG_SCHEMA_VERSION,
        "native": [t.to_dict() for t in NATIVE_TASKS],
        "boundary": [t.to_dict() for t in BOUNDARY_TASKS],
    }
    encoded = json.dumps(data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
