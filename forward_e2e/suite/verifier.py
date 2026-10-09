"""Strict verification rules for task outcomes, JUnit XML, and evidence completeness.

Ensures:
- Automated native PASSED requires exit 0 AND non-empty JUnit for the exact method without skip/fail.
- Package C replacement requires 18 exact tests and 71 exact case markers.
- Acceptance status is always NOT_REVIEWED.
- Zero executed tests in CT is a failure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
import xml.etree.ElementTree as ET

from .catalog import get_task_by_id
from .collector import validate_artifact_index_record
from .evidence_model import (
    EvidenceModelError,
    declared_evidence_model,
    requires_observed_selected_commit,
    resolve_evidence_model,
)
from .models import (
    SUPPORTED_SCHEMA_VERSIONS,
    TOP_LEVEL_SCOPE,
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
    calculate_suite_outcome,
    make_task_run_id,
)
from .runtime import sha256_file
from . import source_snapshot
from .settlement_token import MAINNET_MODE, verify_local_token_evidence, _rejected
from ..execution.file_safety import read_checked_file_bytes, sha256_size_checked_file


# Frozen historical Package C inventory. The replacement credits 71 policy
# cases here; the two R7.2 cases remain a separate reachable native scenario.
PACKAGE_C_REQUIRED_CASES: Set[str] = {
    # r3 routing cases (6 pairs = 12 cases) + healthy (12 cases) = 24
    "r3-handler_error-lock", "r3-handler_error-lock-healthy",
    "r3-handler_error-refund", "r3-handler_error-refund-healthy",
    "r3-malformed_protobuf-lock", "r3-malformed_protobuf-lock-healthy",
    "r3-malformed_protobuf-refund", "r3-malformed_protobuf-refund-healthy",
    "r3-duplicate_routing-lock", "r3-duplicate_routing-lock-healthy",
    "r3-duplicate_routing-refund", "r3-duplicate_routing-refund-healthy",
    # r3 epoch (2 pairs = 4 cases)
    "r3-epoch-lock", "r3-epoch-lock-healthy",
    "r3-epoch-refund", "r3-epoch-refund-healthy",
    # r4 cases (8 kinds x 5 suffixes: probe, e2, e3, terminal-refund, terminal-settle_claim) = 40
    *(
        f"r4-{k}-{suffix}"
        for k in (
            "handler_error", "malformed_protobuf", "oversized_response",
            "missing_nested_summary", "wrong_host", "wrong_epoch",
            "invalid_participant_address", "unsupported_request",
        )
        for suffix in ("probe", "e2", "e3", "terminal-refund", "terminal-settle_claim")
    ),
    # r5-recover (native, probe, e2, ledger, refund, settle, repeat) = 7
    "r5-recover-native", "r5-recover-probe", "r5-recover-e2",
    "r5-recover-ledger", "r5-recover-refund", "r5-recover-settle", "r5-recover-repeat",
    # r5-cancel (native, probe, e2, e3, ledger, terminal-refund, terminal-settle_claim) = 7
    "r5-cancel-native", "r5-cancel-probe", "r5-cancel-e2", "r5-cancel-e3",
    "r5-cancel-ledger", "r5-cancel-terminal-refund", "r5-cancel-terminal-settle_claim",
    # r6.3, bank-fault, bank-retry = 3
    "r6.3", "r7.2-fault", "r7.2-retry",
}

PACKAGE_C_POLICY_CASES: Set[str] = PACKAGE_C_REQUIRED_CASES - {
    "r7.2-fault",
    "r7.2-retry",
}

PACKAGE_C_POLICY_TESTS: Set[str] = {
    *(f"contract::tests::c_policy_{kind}" for kind in (
        "handler_error", "malformed_protobuf", "oversized_response",
        "missing_nested_summary", "wrong_host", "wrong_epoch",
        "invalid_participant_address", "unsupported_request",
    )),
    *(f"contract::tests::c_routing_{kind}_{operation}"
      for kind in ("handler_error", "malformed_protobuf", "duplicate_routing", "epoch")
      for operation in ("lock", "refund")),
    "c_recovery_before_deadline_preserves_ledgers_and_settles_once",
    "c_network_unconfirmed_refund_rolls_back_retries_and_keeps_terminal_host_only_economics",
}

# The 9 required CosmWasm ExternalQuerier ABI query_chain cases
WASM_ABI_REQUIRED_CASES: Set[str] = {
    "malformed-envelope",
    "invalid-envelope-shape",
    "invalid_request",
    "unknown",
    "no_such_contract",
    "no_such_code",
    "unsupported_request",
    "contract-error-control",
    "success-control",
}

GO_BOUNDARY_REQUIRED_TESTS: Set[str] = {
    "TestToQuerierResultClassifiesVMSystemErrors",
    "TestStrictPlanValidation",
}

# Mirrors of the boundary constants in scripts/acceptance_harness.py. The producer
# enforces them while writing evidence; the verifier must enforce the very same
# offsets while reading it, otherwise a late refund proves an early boundary.
DEFAULT_DENOM = "ngonka"
CLAIM_EXPIRY_DELAY_EPOCHS = 2
NETWORK_UNCONFIRMED_DELAY_EPOCHS = 3
CLAIMED_REFUND_GAS_LIMITS = (50_000, 100_000, 150_000, 250_000, 500_000, 1_000_000)
CLAIMED_REFUND_SUFFICIENT_GAS = 2_000_000
CLAIMED_REFUND_OUTER_GAS = 2_000_000


def normalize_test_name(name: str) -> str:
    """Normalize test method name by stripping trailing () and whitespace."""
    return re.sub(r"\(\s*\)$", "", name).strip()


def verify_junit_xml(
    junit_path: Path,
    expected_method: str,
    exact_match: bool = True,
    *,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[bool, List[str], Optional[str]]:
    """Parse JUnit XML and verify that the expected test method executed and passed without skip/failure.

    Normalizes method names by stripping trailing '()' before comparing.
    When exact_match=True, performs strict equality matching to prevent collisions
    such as 'exact at E' falsely matching 'exact at E plus 4'.

    ``expected_method`` is the single name frozen in the package's own
    ``suite-plan.json``. There is deliberately no table of "equivalent" method
    names here: a package planned before a Kotlin method was renamed carries the
    old name and its JUnit report carries the old name, so it matches on its
    own, and a package planned after the rename must not be satisfiable by a
    JUnit file that names a method the current harness cannot produce.

    Returns: (is_passed, observed_test_names, error_detail)
    """
    if not junit_path.is_file():
        return False, [], f"JUnit XML file not found: {junit_path}"

    try:
        root = (
            ET.fromstring(artifact_reader(junit_path))
            if artifact_reader is not None else ET.parse(junit_path).getroot()
        )
    except Exception as exc:
        return False, [], f"Malformed JUnit XML: {exc}"

    testcases = root.findall(".//testcase")
    if not testcases:
        return False, [], "JUnit XML contains 0 testcases"

    observed: List[str] = []
    matched_and_passed = False
    norm_expected = normalize_test_name(expected_method)

    for tc in testcases:
        raw_name = tc.get("name", "")
        clean_name = normalize_test_name(raw_name)
        classname = tc.get("classname", "")
        full_name = f"{classname}.{clean_name}" if classname else clean_name
        observed.append(clean_name)

        if exact_match:
            is_match = (
                clean_name == norm_expected
                or full_name == norm_expected
                or clean_name.lower() == norm_expected.lower()
                or clean_name == f"marketplace {norm_expected}"
                or clean_name.lower() == f"marketplace {norm_expected}".lower()
                or (norm_expected.startswith("marketplace ") and clean_name == norm_expected[len("marketplace "):])
                or (norm_expected.startswith("marketplace ") and clean_name.lower() == norm_expected[len("marketplace "):].lower())
            )
        else:
            is_match = norm_expected in clean_name or norm_expected in full_name

        if is_match:
            failures = tc.findall("failure")
            errors = tc.findall("error")
            skipped = tc.findall("skipped")

            if failures:
                msg = failures[0].get("message") or failures[0].text or "failed"
                return False, observed, f"Testcase {clean_name} failed: {msg}"
            if errors:
                msg = errors[0].get("message") or errors[0].text or "error"
                return False, observed, f"Testcase {clean_name} errored: {msg}"
            if skipped:
                return False, observed, f"Testcase {clean_name} was skipped"

            matched_and_passed = True

    if not matched_and_passed:
        return False, observed, f"Expected test method {expected_method!r} not found in JUnit XML"

    return True, observed, None


def _as_int(value: Any) -> Optional[int]:
    """Coerces the producer's mixed int/str numeric fields into an int.

    scripts/acceptance_harness.py writes tx heights as strings (filtered_tx) and
    epoch brackets as ints (assert_tx_epoch_bracket), so both must be accepted
    while booleans and junk values must not silently pass as numbers.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _obj(value: Any) -> Dict[str, Any]:
    """Returns the value when it is a non-empty dict, otherwise an empty dict."""
    return dict(value) if isinstance(value, dict) and value else {}


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _protobuf_bool(container: Any, field: str) -> Optional[bool]:
    """Decodes a proto3 JSON bool exactly like acceptance_harness.protobuf_bool.

    proto3 JSON may omit a false default, so a missing key decodes to False.
    Any other representation (notably the non-empty string "false", which is
    truthy for bool()) is a decoding error for the producer and is reported
    here as None so callers reject it instead of guessing.
    """
    if not isinstance(container, Mapping):
        return None
    raw = container.get(field, False)
    if not isinstance(raw, bool):
        return None
    return raw


def _tx_included(tx: Mapping[str, Any]) -> bool:
    """A successful transaction must be included: code 0, real hash, real height."""
    if not isinstance(tx, dict) or not tx:
        return False
    height = _as_int(tx.get("height"))
    code = tx.get("code")
    return (
        isinstance(code, int)
        and not isinstance(code, bool)
        and code == 0
        and _nonempty_str(tx.get("tx_hash"))
        and height is not None
        and height > 0
    )


def _attempt_rejected_and_included(
    attempt: Mapping[str, Any],
    expected_codespace: Optional[str] = "wasm",
) -> bool:
    """A rejection must be proven on-chain (DeliverTx), not by simulation."""
    if not isinstance(attempt, dict) or not attempt:
        return False
    code = attempt.get("code")
    height = _as_int(attempt.get("height"))
    if attempt.get("layer") != "deliver_tx":
        return False
    if not isinstance(code, int) or isinstance(code, bool) or code == 0:
        return False
    if expected_codespace is not None and attempt.get("codespace") != expected_codespace:
        return False
    if not _nonempty_str(attempt.get("tx_hash")):
        return False
    return height is not None and height > 0


def _bracket_binds_tx(
    bracket: Any,
    expected_epoch: Optional[int],
    tx: Mapping[str, Any],
) -> bool:
    """The epoch bracket must be complete and bound to the transaction height.

    A deleted or emptied bracket, a bracket that straddles an epoch change, or a
    bracket that is not anchored on the transaction's own height proves nothing.
    """
    if expected_epoch is None:
        return False
    if not isinstance(bracket, dict) or not bracket:
        return False
    if bracket.get("same_epoch") is not True:
        return False
    epoch = _as_int(bracket.get("epoch"))
    before_h = _as_int(bracket.get("before_height"))
    tx_h = _as_int(bracket.get("tx_height"))
    after_h = _as_int(bracket.get("after_height"))
    if epoch is None or before_h is None or tx_h is None or after_h is None:
        return False
    if epoch != expected_epoch:
        return False
    if not (before_h <= tx_h <= after_h):
        return False
    real_height = _as_int((tx or {}).get("height"))
    return real_height is not None and real_height == tx_h


def _snapshot_sections_present(snapshot: Mapping[str, Any]) -> bool:
    """scenario_financial_snapshot always records state and both balance maps."""
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    if not _obj(snapshot.get("state")):
        return False
    if not isinstance(snapshot.get("cw20"), dict) or not snapshot.get("cw20"):
        return False
    if not isinstance(snapshot.get("bank_ngonka"), dict) or not snapshot.get("bank_ngonka"):
        return False
    return True


def _validate_successful_lock_phase(
    phase: Mapping[str, Any],
    terms: Mapping[str, Any],
    offset: int,
) -> bool:
    """Validates lock_exact_e / lock_e_plus_4 against the scenario's own terms.

    The target epoch is mandatory and is taken from the terms of the scenario
    that owns this phase, never from another scenario or from a flat top-level
    field.
    """
    target_epoch = _as_int(_obj(terms).get("target_epoch"))
    if target_epoch is None:
        return False

    expected_epoch = _as_int(phase.get("expected_epoch"))
    if expected_epoch is None or expected_epoch != target_epoch + offset:
        return False

    tx = _obj(phase.get("tx"))
    if not _tx_included(tx):
        return False
    if not _bracket_binds_tx(phase.get("epoch_bracket"), expected_epoch, tx):
        return False

    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
        return False

    before_state = _obj(before.get("state"))
    after_state = _obj(after.get("state"))
    if before_state.get("status") != "funded" or after_state.get("status") != "locked":
        return False
    if before_state.get("recipient_locked") is not False:
        return False
    if after_state.get("recipient_locked") is not True:
        return False

    before_bank = _obj(before.get("bank_ngonka"))
    after_bank = _obj(after.get("bank_ngonka"))
    if "deal" not in before_bank or "deal" not in after_bank:
        return False
    if before_bank != after_bank:
        return False

    if before.get("cw20") != after.get("cw20"):
        return False
    if before.get("foreign_cw20") != after.get("foreign_cw20"):
        return False

    config_before = phase.get("config_before")
    config_after = phase.get("config_after")
    if not isinstance(config_before, dict) or not config_before:
        return False
    if config_before != config_after:
        return False

    return True


def _validate_lock_rejected_phase(
    phase: Mapping[str, Any],
    terms: Mapping[str, Any],
) -> bool:
    """Validates lock_e_plus_5_rejected: included rejection at E+5, state intact."""
    target_epoch = _as_int(_obj(terms).get("target_epoch"))
    if target_epoch is None:
        return False

    expected_epoch = _as_int(phase.get("expected_epoch"))
    if expected_epoch is None or expected_epoch != target_epoch + 5:
        return False

    attempt = _obj(phase.get("attempt"))
    if not _attempt_rejected_and_included(attempt):
        return False
    if not _bracket_binds_tx(phase.get("epoch_bracket"), expected_epoch, attempt):
        return False

    raw_log = str(attempt.get("raw_log", "")).lower()
    if "lock window is closed" not in raw_log:
        return False
    if "out of gas" in raw_log:
        return False

    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
        return False

    before_state = _obj(before.get("state"))
    after_state = _obj(after.get("state"))
    if before_state.get("status") != "funded" or after_state.get("status") != "funded":
        return False
    if before_state.get("recipient_locked") is not False:
        return False
    if after_state.get("recipient_locked") is not False:
        return False
    if before_state != after_state:
        return False
    if before.get("cw20") != after.get("cw20"):
        return False

    return True


def _validate_unfunded_lock_boundaries(data: Mapping[str, Any]) -> bool:
    """Require native absence, an E+4 lock and an included no-op rejection at E+5."""
    scenarios = _obj(data.get("scenarios"))
    valid_records: Dict[str, Dict[str, Any]] = {}
    for name in ("lock-e-plus-4", "lock-e-plus-5"):
        record = _obj(scenarios.get(name))
        terms = _obj(record.get("terms"))
        target = _as_int(terms.get("target_epoch"))
        if target is None:
            return False
        valid_records[name] = {"record": record, "target": target}
    if valid_records["lock-e-plus-4"]["target"] != valid_records["lock-e-plus-5"]["target"]:
        return False
    target = valid_records["lock-e-plus-4"]["target"]

    e4_record = valid_records["lock-e-plus-4"]["record"]
    e4_phases = [p for p in e4_record.get("phases", []) if isinstance(p, dict)] \
        if isinstance(e4_record.get("phases"), list) else []
    e4_matches = [p for p in e4_phases if p.get("name") == "lock"]
    if len(e4_matches) != 1:
        return False
    e4 = e4_matches[0]
    before = _obj(e4.get("state_before"))
    after = _obj(e4.get("state_after"))
    tx = _obj(e4.get("tx"))
    bracket = e4.get("epoch_bracket")
    e4_recipients = _obj(e4.get("recipient_query")).get("entries")
    deal = _obj(e4_record.get("contracts")).get("deal")
    if not isinstance(e4_recipients, list) or not isinstance(deal, str) or not deal:
        return False
    exact_recipient = [
        item for item in e4_recipients
        if isinstance(item, Mapping)
        and _as_int(item.get("epoch")) == target
        and item.get("recipient") == deal
    ]
    if len(exact_recipient) != 1:
        return False
    if (
        _as_int(e4.get("expected_epoch")) != target + 4
        or not _tx_included(tx)
        or not _bracket_binds_tx(bracket, target + 4, tx)
        or not _no_bank_transfer_involving(tx, deal)
    ):
        return False
    if (
        before.get("status") != "open"
        or before.get("buyer", object()) is not None
        or before.get("recipient_locked") is not False
        or after.get("status") != "locked"
        or after.get("buyer", object()) is not None
        or after.get("recipient_locked") is not True
    ):
        return False

    e5_record = valid_records["lock-e-plus-5"]["record"]
    e5_phases = [p for p in e5_record.get("phases", []) if isinstance(p, dict)] \
        if isinstance(e5_record.get("phases"), list) else []
    e5_matches = [p for p in e5_phases if p.get("name") == "lock_rejected"]
    if len(e5_matches) != 1:
        return False
    e5 = e5_matches[0]
    attempt = _obj(e5.get("attempt"))
    bracket = e5.get("epoch_bracket")
    if (
        e5.get("routing_expectation") != "pruned"
        or _as_int(e5.get("expected_epoch")) != target + 5
        or not _attempt_rejected_and_included(attempt)
        or not _bracket_binds_tx(bracket, target + 5, attempt)
        or "lock window is closed" not in str(attempt.get("raw_log", "")).lower()
    ):
        return False
    recipient_query = e5.get("recipient_query")
    if not isinstance(recipient_query, Mapping):
        return False
    # QueryListClaimRecipientsResponse contains only a repeated entries field.
    # Native protobuf JSON omits an empty list, returning {}. Require that
    # explicit query result; a missing document is never proof of pruning.
    e5_recipients = [] if not recipient_query else recipient_query.get("entries")
    if not isinstance(e5_recipients, list) or any(
        isinstance(item, Mapping) and _as_int(item.get("epoch")) == target
        for item in e5_recipients
    ):
        return False
    before_snapshot = _obj(e5.get("before"))
    after_snapshot = _obj(e5.get("after"))
    if (
        not _snapshot_sections_present(before_snapshot)
        or before_snapshot != after_snapshot
        or _obj(before_snapshot.get("state")).get("status") != "open"
        or _obj(before_snapshot.get("state")).get("buyer", object()) is not None
        or _obj(before_snapshot.get("state")).get("recipient_locked") is not False
    ):
        return False
    return True


def _validate_claimed_refund_gas_sweep(data: Mapping[str, Any]) -> bool:
    """Recompute the native claimed-Refund gas sweep from its recorded attempts."""
    scenarios = _obj(data.get("scenarios"))
    scenario = _obj(scenarios.get("gas-claimed"))
    terms = _obj(scenario.get("terms"))
    accounts = _obj(scenario.get("accounts"))
    contracts = _obj(scenario.get("contracts"))
    root_accounts = _obj(data.get("accounts"))
    root_contracts = _obj(data.get("contracts"))
    target = _as_int(terms.get("target_epoch"))
    host = accounts.get("host")
    buyer = accounts.get("buyer") or root_accounts.get("buyer")
    deal = contracts.get("deal")
    caller = contracts.get("caller") or root_contracts.get("caller")
    if (
        target is None or not _nonempty_str(host) or not _nonempty_str(buyer)
        or not _nonempty_str(deal) or not _nonempty_str(caller)
        or not _nonempty_str(accounts.get("fee_recipient"))
    ):
        return False

    phases = scenario.get("phases")
    if not isinstance(phases, list):
        return False
    claimed_phases = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "native_auto_claim"]
    lock_phases = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "lock"]
    sweep_phases = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "claimed_refund_gas_sweep"]
    if len(claimed_phases) != 1 or len(lock_phases) != 1 or len(sweep_phases) != 1:
        return False
    claimed_phase, lock_phase, sweep = claimed_phases[0], lock_phases[0], sweep_phases[0]

    summary = _obj(_obj(claimed_phase.get("summary")).get("epochPerformanceSummary"))
    claimed_recipients = _obj(claimed_phase.get("recipient_query")).get("entries")
    if not isinstance(claimed_recipients, list):
        return False
    matching_recipients = [
        item for item in claimed_recipients
        if isinstance(item, Mapping)
        and _as_int(item.get("epoch")) == target
        and item.get("recipient") == deal
    ]
    work = _as_int(summary.get("earned_coins", 0))
    reward = _as_int(summary.get("rewarded_coins", 0))
    if (
        claimed_phase.get("level") != "live_network"
        or summary.get("claimed") is not True
        or _as_int(summary.get("epoch_index")) != target
        or summary.get("participant_id") != host
        or work is None or reward is None or work < 0 or reward < 0 or work + reward <= 0
        or len(matching_recipients) != 1
        or _obj(claimed_phase.get("expected")).get("claimed") is not True
        or _obj(claimed_phase.get("expected")).get("positive_total") is not True
        or _as_int(_obj(claimed_phase.get("actual")).get("total_ngonka")) != work + reward
        or _obj(claimed_phase.get("actual")).get("exact_recipient") is not True
    ):
        return False

    lock_tx = _obj(lock_phase.get("tx"))
    lock_before = _obj(lock_phase.get("state_before"))
    lock_after = _obj(lock_phase.get("state_after"))
    lock_epoch = _as_int(_obj(lock_phase.get("epoch_bracket")).get("epoch"))
    lock_recipients = _obj(lock_phase.get("recipient_query")).get("entries")
    bracket = lock_phase.get("epoch_bracket")
    if not isinstance(lock_recipients, list):
        return False
    if (
        lock_epoch is None or not target <= lock_epoch <= target + 4
        or not _tx_included(lock_tx)
        or not _bracket_binds_tx(bracket, lock_epoch, lock_tx)
        or not _no_bank_transfer_involving(lock_tx, deal)
        or lock_before.get("status") != "funded"
        or lock_before.get("buyer") != buyer
        or lock_before.get("recipient_locked") is not False
        or lock_after.get("status") != "locked"
        or lock_after.get("buyer") != buyer
        or lock_after.get("recipient_locked") is not True
        or len([
            item for item in lock_recipients
            if isinstance(item, Mapping)
            and _as_int(item.get("epoch")) == target
            and item.get("recipient") == deal
        ]) != 1
    ):
        return False

    pre = _obj(sweep.get("preconditions"))
    sweep_summary = _obj(_obj(pre.get("summary")).get("epochPerformanceSummary"))
    current_epoch = _as_int(pre.get("current_epoch"))
    minimum_epoch = target + NETWORK_UNCONFIRMED_DELAY_EPOCHS
    fee_payer = pre.get("fee_payer")
    tracked_addresses = {
        "host": host,
        "buyer": buyer,
        "fee_recipient": accounts.get("fee_recipient"),
        "deal": deal,
    }
    expected_fee_roles = sorted(role for role, address in tracked_addresses.items() if address == fee_payer)
    fee_roles = pre.get("fee_payer_roles")
    if (
        sweep.get("level") != "live_network"
        or _as_int(pre.get("target_epoch")) != target
        or current_epoch is None or current_epoch < minimum_epoch
        or _as_int(pre.get("minimum_epoch")) != minimum_epoch
        or pre.get("current_epoch_at_least_e_plus_3") is not True
        or pre.get("claimed") is not True
        or pre.get("summary_identity_valid") is not True
        or sweep_summary != summary
        or _as_int(pre.get("work_ngonka")) != work
        or _as_int(pre.get("reward_ngonka")) != reward
        or _as_int(pre.get("total_claim_ngonka")) != work + reward
        or pre.get("positive_claim") is not True
        or not _nonempty_str(fee_payer)
        or fee_roles != expected_fee_roles
        or sweep.get("gas_limits") != list(CLAIMED_REFUND_GAS_LIMITS)
        or _as_int(sweep.get("sufficient_gas")) != CLAIMED_REFUND_SUFFICIENT_GAS
        or _as_int(sweep.get("outer_gas")) != CLAIMED_REFUND_OUTER_GAS
    ):
        return False

    baseline = _obj(sweep.get("baseline"))
    final = _obj(sweep.get("final"))
    if (
        not _snapshot_sections_present(baseline)
        or not _snapshot_sections_present(final)
        or baseline.get("state") != lock_after
        or baseline.get("state") != final.get("state")
        or baseline.get("cw20") != final.get("cw20")
        or baseline.get("foreign_cw20") != final.get("foreign_cw20")
        or _obj(baseline.get("state")).get("buyer") != buyer
    ):
        return False
    baseline_bank = _obj(baseline.get("bank_ngonka"))
    final_bank = _obj(final.get("bank_ngonka"))
    if not baseline_bank or set(baseline_bank) != set(final_bank):
        return False

    attempts = sweep.get("attempts")
    if not isinstance(attempts, list):
        return False
    expected_keys = {
        *(('direct', gas) for gas in CLAIMED_REFUND_GAS_LIMITS),
        ('direct_sufficient', CLAIMED_REFUND_SUFFICIENT_GAS),
        ('caller_forward', CLAIMED_REFUND_OUTER_GAS),
        *(('submessage_reply', gas) for gas in CLAIMED_REFUND_GAS_LIMITS),
    }
    attempt_by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
    previous_fee_deltas = {role: 0 for role in expected_fee_roles}
    for item in attempts:
        if not isinstance(item, dict):
            return False
        mode = item.get("mode")
        gas_limit = _as_int(item.get("gas_limit"))
        key = (mode, gas_limit) if isinstance(mode, str) and gas_limit is not None else None
        if key is None or key not in expected_keys or key in attempt_by_key:
            return False
        attempt_by_key[key] = item
        tx = _obj(item.get("outer_tx" if mode == "submessage_reply" else "tx"))
        expected_tx_gas = CLAIMED_REFUND_OUTER_GAS if mode == "submessage_reply" else gas_limit
        if _as_int(tx.get("gas_wanted")) != expected_tx_gas or not _no_bank_transfer_involving(tx, deal):
            return False
        is_submessage = mode == "submessage_reply"
        if is_submessage:
            if not _tx_included(tx):
                return False
            reply = _obj(item.get("reply"))
            if reply.get("success") is not False or not _nonempty_str(reply.get("error")):
                return False
        elif not _attempt_rejected_and_included(tx, expected_codespace=None):
            return False
        fee_deltas = item.get("cumulative_fee_payer_deltas_ngonka")
        if not isinstance(fee_deltas, Mapping) or set(fee_deltas) != set(expected_fee_roles):
            return False
        for role in expected_fee_roles:
            delta = _as_int(fee_deltas.get(role))
            if delta is None or delta > previous_fee_deltas[role]:
                return False
            previous_fee_deltas[role] = delta
        if mode == "direct_sufficient":
            actual_reason = _obj(item.get("actual_reason"))
            if (
                f"native claim for target epoch {target} is already confirmed"
                not in str(tx.get("raw_log", "")).lower()
                or "out of gas" in str(tx.get("raw_log", "")).lower()
                or actual_reason.get("reason") != "claimed"
                or actual_reason.get("state_status") != "locked"
                or actual_reason.get("layer") != "deliver_tx"
                # The harness records the stable error prefix in this field;
                # the complete epoch-specific rejection is proved by raw_log.
                or actual_reason.get("matched_contract_error") != "native claim for target epoch"
            ):
                return False
    if set(attempt_by_key) != expected_keys:
        return False

    out_of_gas_keys: Set[Tuple[str, int]] = set()
    for key, item in attempt_by_key.items():
        mode, _gas_limit = key
        if mode == "direct_sufficient":
            continue
        tx = _obj(item.get("outer_tx" if mode == "submessage_reply" else "tx"))
        message = (
            str(_obj(item.get("reply")).get("error", ""))
            if mode == "submessage_reply" else str(tx.get("raw_log", ""))
        )
        if "out of gas" in message.lower():
            out_of_gas_keys.add(key)
    recorded_oog = _obj(sweep.get("out_of_gas"))
    observations = recorded_oog.get("observations")
    if recorded_oog.get("reached") is not True or not isinstance(observations, list) or not observations:
        return False
    observed_oog_keys: Set[Tuple[str, int]] = set()
    for observation in observations:
        if not isinstance(observation, Mapping):
            return False
        mode = observation.get("mode")
        gas_limit = _as_int(observation.get("gas_limit"))
        key = (mode, gas_limit) if isinstance(mode, str) and gas_limit is not None else None
        if key is None or key not in out_of_gas_keys or key in observed_oog_keys:
            return False
        item = attempt_by_key[key]
        if mode == "submessage_reply":
            if (
                observation.get("outer_tx") != item.get("outer_tx")
                or observation.get("reply") != item.get("reply")
            ):
                return False
        elif observation.get("tx") != item.get("tx"):
            return False
        observed_oog_keys.add(key)
    if observed_oog_keys != out_of_gas_keys or not out_of_gas_keys:
        return False

    sufficient = attempt_by_key[('direct_sufficient', CLAIMED_REFUND_SUFFICIENT_GAS)]
    sufficient_proof = _obj(sweep.get("sufficient_gas_contract_rejection"))
    if (
        sufficient_proof.get("reached") is not True
        or sufficient_proof.get("attempt") != sufficient.get("tx")
        or sufficient_proof.get("actual_reason") != sufficient.get("actual_reason")
    ):
        return False

    for role in baseline_bank:
        before_amount = _as_int(baseline_bank.get(role))
        after_amount = _as_int(final_bank.get(role))
        if before_amount is None or after_amount is None:
            return False
        delta = after_amount - before_amount
        if role in expected_fee_roles:
            if delta > 0 or delta != previous_fee_deltas[role]:
                return False
        elif delta != 0:
            return False
    return True


def _validate_r1_refund_rejected_phase(
    phase: Mapping[str, Any],
    terms: Mapping[str, Any],
) -> bool:
    """Validates r1_1_refund_e_plus_5_rejected as written by scripts/acceptance_harness.py.

    The phase has no expected_epoch field: the exclusive E+5 boundary is proven
    by the epoch bracket of the rejected transaction against the owning
    scenario's target epoch.
    """
    target_epoch = _as_int(_obj(terms).get("target_epoch"))
    if target_epoch is None:
        return False

    if phase.get("status") != "PASS":
        return False
    if phase.get("semantic_error"):
        return False
    invariant_errors = phase.get("invariant_errors")
    if not isinstance(invariant_errors, list) or invariant_errors:
        return False

    attempt = _obj(phase.get("attempt"))
    if not _attempt_rejected_and_included(attempt):
        return False
    if not _bracket_binds_tx(phase.get("epoch_bracket"), target_epoch + 5, attempt):
        return False

    raw_log = str(attempt.get("raw_log", "")).lower()
    if "refund routing-proof window is closed" not in raw_log:
        return False
    if "out of gas" in raw_log:
        return False

    expected = _obj(phase.get("expected"))
    if expected.get("contract_error") != "RefundWindowClosed":
        return False
    if expected.get("state") != "funded":
        return False
    if expected.get("recipient_locked") is not False:
        return False
    if expected.get("simulation_accepted") is not False:
        return False
    if expected.get("arbitrary_error_accepted") is not False:
        return False
    if expected.get("deposit_on_deal") is not True:
        return False

    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
        return False

    before_state = _obj(before.get("state"))
    if before_state.get("status") != "funded":
        return False
    if before_state.get("recipient_locked") is not False:
        return False

    # A rejected refund must leave state and every balance map untouched.
    if before_state != _obj(after.get("state")):
        return False
    if before.get("bank_ngonka") != after.get("bank_ngonka"):
        return False
    if before.get("cw20") != after.get("cw20"):
        return False
    if before.get("foreign_cw20") != after.get("foreign_cw20"):
        return False

    return True


def _validate_b3_foreign_native_phase(phase: Mapping[str, Any]) -> Set[str]:
    """Validates b3_foreign_native_successful_release.

    B3 intentionally leaves a positive foreign-denom balance on the Deal: the
    zero balance applies to GNK (ngonka), never to the foreign denom. Funding,
    foreign preservation, and GNK conservation are proven separately.
    """
    checkpoints: Set[str] = set()

    fixture = _obj(phase.get("fixture"))
    funding = _obj(phase.get("foreign_native_funding"))
    release = _obj(phase.get("release"))

    amount = _as_int(fixture.get("amount"))
    denom = fixture.get("denom")

    funding_tx = _obj(funding.get("tx"))
    funding_before = _obj(funding.get("before"))
    funding_after = _obj(funding.get("after"))
    source_before = _as_int(fixture.get("genesis_source_balance_before"))
    source_after = _as_int(fixture.get("genesis_source_balance_after"))

    funding_ok = (
        amount is not None
        and amount > 0
        and _nonempty_str(denom)
        and denom != "ngonka"
        and _tx_included(funding_tx)
        and bool(_obj(funding.get("event")))
        and _as_int(funding_before.get("deal")) == 0
        and _as_int(funding_after.get("deal")) == amount
        and source_before is not None
        and source_after is not None
        and source_before - source_after == amount
        and all(
            _as_int(funding_after.get(role)) == _as_int(funding_before.get(role))
            for role in ("host", "buyer", "fee_recipient", "caller")
            if role in funding_before or role in funding_after
        )
    )
    if funding_ok:
        checkpoints.add("foreign_native_funding_verified")

    release_tx = _obj(release.get("tx"))
    before = _obj(release.get("before"))
    after = _obj(release.get("after"))
    actual = _obj(release.get("actual"))
    expected = _obj(release.get("expected"))
    before_ngonka = _int_map(_obj(before.get("ngonka")))
    after_ngonka = _int_map(_obj(after.get("ngonka")))
    foreign_before = _obj(before.get("foreign_native"))
    foreign_after = _obj(after.get("foreign_native"))

    release_included = _tx_included(release_tx) and _bracket_binds_tx(
        release.get("stable_epoch"),
        _as_int(_obj(release.get("stable_epoch")).get("epoch")),
        release_tx,
    )

    # Foreign native denom must survive the release untouched and positive.
    foreign_preserved = (
        funding_ok
        and release_included
        and bool(foreign_before)
        and foreign_before == foreign_after
        and _as_int(foreign_after.get("deal")) == amount
        and _as_int(actual.get("foreign_native_deal_after")) == amount
    )
    if foreign_preserved:
        checkpoints.add("foreign_native_balance_preserved")

    # GNK conservation: the Deal's ngonka balance is the one that must reach 0.
    available = _as_int(actual.get("available_ngonka"))
    buyer_delta = _as_int(actual.get("buyer_delta"))
    host_delta = _as_int(actual.get("host_delta"))
    expected_buyer = _as_int(expected.get("buyer_delta"))
    expected_host = _as_int(expected.get("host_delta"))
    expected_released = _as_int(expected.get("released_total"))
    expected_buyer_released = _as_int(expected.get("buyer_released"))
    expected_host_released = _as_int(expected.get("host_released"))
    state_before = _obj(release.get("state_before"))
    state_after = _obj(release.get("state_after"))
    snapshot_deltas_ok = (
        before_ngonka is not None
        and after_ngonka is not None
        and all(role in before_ngonka and role in after_ngonka
                for role in ("deal", "buyer", "host", "fee_recipient", "caller"))
        and available == before_ngonka.get("deal")
        and buyer_delta == after_ngonka.get("buyer", 0) - before_ngonka.get("buyer", 0)
        and host_delta == after_ngonka.get("host", 0) - before_ngonka.get("host", 0)
        and after_ngonka.get("deal") == 0
        and after_ngonka.get("fee_recipient") == before_ngonka.get("fee_recipient")
        and after_ngonka.get("caller") == before_ngonka.get("caller")
    )
    state_counters_ok = (
        state_before.get("status") == "releasing"
        and state_after.get("status") == "releasing"
        and _as_int(state_before.get("released_total_ngonka")) == 0
        and _as_int(state_before.get("buyer_released_ngonka")) == 0
        and _as_int(state_before.get("host_released_ngonka")) == 0
        and _as_int(state_after.get("released_total_ngonka")) == available
        and _as_int(state_after.get("buyer_released_ngonka")) == buyer_delta
        and _as_int(state_after.get("host_released_ngonka")) == host_delta
    )
    release_ok = (
        release_included
        and snapshot_deltas_ok
        and state_counters_ok
        and available is not None
        and available > 0
        and buyer_delta is not None
        and buyer_delta >= 0
        and host_delta is not None
        and host_delta >= 0
        and buyer_delta + host_delta == available
        and expected_buyer == buyer_delta
        and expected_host == host_delta
        and expected_released == available
        and expected_buyer_released == buyer_delta
        and expected_host_released == host_delta
        and _obj(before.get("settlement_cw20")) == _obj(after.get("settlement_cw20"))
        and _obj(before.get("foreign_cw20")) == _obj(after.get("foreign_cw20"))
    )
    if release_ok and foreign_preserved:
        checkpoints.add("release_completed")

    return checkpoints


def _validate_cw20_fault_rollbacks(
    faults: Any, phase: Mapping[str, Any], scope: Mapping[str, Any],
    required_positions: Sequence[int] = (1, 2, 3),
) -> bool:
    """Validates exactly the selected recipient-bound CW20 rejections.

    Every entry must carry a typed fault target, an included DeliverTx
    rejection, unchanged financial snapshots and unchanged pending obligations.
    Empty or partially deleted entries must not grant the checkpoint.
    """
    if not isinstance(faults, list) or len(faults) != len(required_positions):
        return False

    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))
    expected = _obj(_obj(phase.get("expected")).get("cw20_deltas"))
    actual = _obj(_obj(phase.get("actual")).get("cw20_deltas"))
    before_cw20 = _int_map(_obj(phase.get("before")).get("cw20"))
    after_cw20 = _int_map(_obj(phase.get("after")).get("cw20"))
    payments = _obj(phase.get("settlement_payments"))
    settled_state = _obj(_obj(phase.get("after")).get("deal_state"))
    settle_event = _event_attributes(_obj(phase.get("settle_tx")), "wasm-claim_settled")
    expected_state = _obj(phase.get("expected"))
    actual_state = _obj(_obj(phase.get("actual")).get("state"))
    payout_order = ("host", "fee_recipient", "buyer")
    if (
        not _nonempty_str(contracts.get("cw20"))
        or not _nonempty_str(contracts.get("deal"))
        or not before_cw20 or not after_cw20 or not settled_state
        or not payments or not settle_event or not actual_state
        or settle_event.get("_contract_address") != contracts["deal"]
        or settle_event.get("deal") != contracts["deal"]
        or settle_event.get("host") != accounts.get("host")
        or settle_event.get("buyer") != accounts.get("buyer")
        or set(expected) != set(payout_order)
        or set(actual) != set(payout_order)
    ):
        return False
    for field, value in actual_state.items():
        if (
            field not in expected_state or field not in settled_state
            or _normalized(value) != _normalized(expected_state[field])
            or _normalized(value) != _normalized(settled_state[field])
        ):
            return False
    for field in ("gross_usdt", "fee_usdt", "host_net_usdt", "buyer_refund_usdt"):
        if _as_int(settle_event.get(field)) != _as_int(expected_state.get(field)):
            return False
    expected_amounts: Dict[str, int] = {}
    for role in payout_order:
        amount = _as_int(expected.get(role))
        if (
            not _nonempty_str(accounts.get(role))
            or amount is None or amount <= 0
            or _as_int(actual.get(role)) != amount
            or role not in before_cw20 or role not in after_cw20
            or after_cw20[role] - before_cw20[role] != amount
        ):
            return False
        payment = _obj(payments.get("fee" if role == "fee_recipient" else role))
        if (
            payment.get("recipient") != accounts[role]
            or _as_int(payment.get("accrued_micro_usdt")) != amount
            or _as_int(payment.get("pending_micro_usdt")) != amount
            or _as_int(payment.get("paid_micro_usdt")) != 0
        ):
            return False
        expected_amounts[role] = amount
    if (
        "deal" not in before_cw20 or after_cw20.get("deal") != 0
        or before_cw20["deal"] - after_cw20["deal"] != sum(expected_amounts.values())
        or _as_int(_obj(phase.get("expected")).get("deal_outflow"))
        != sum(expected_amounts.values())
        or _as_int(_obj(phase.get("actual")).get("deal_outflow"))
        != sum(expected_amounts.values())
    ):
        return False

    positions: Set[int] = set()
    recipients: Set[str] = set()
    roles: Set[str] = set()
    tx_hashes: Set[str] = set()
    all_hashes: Set[str] = {str(_obj(phase.get("settle_tx")).get("tx_hash"))}

    for entry in faults:
        if not isinstance(entry, dict) or not entry:
            return False

        fault = _obj(entry.get("fault"))
        position = _as_int(fault.get("outgoing_transfer_index"))
        recipient = fault.get("recipient")
        role = fault.get("role")
        amount = _as_int(fault.get("amount"))
        if position is None or position not in (1, 2, 3):
            return False
        if not _nonempty_str(recipient) or not _nonempty_str(role):
            return False
        if amount is None or amount <= 0:
            return False
        if not _nonempty_str(fault.get("contract")):
            return False
        expected_role = payout_order[position - 1]
        if (
            role != expected_role
            or recipient != accounts[expected_role]
            or amount != expected_amounts[expected_role]
            or fault.get("contract") != contracts["cw20"]
            or entry.get("level") != "native_fault_injection"
        ):
            return False
        positions.add(position)
        recipients.add(str(recipient))
        roles.add(str(role))

        setup_tx = _obj(entry.get("setup_tx"))
        clear_tx = _obj(entry.get("clear_tx"))
        if not _tx_included(setup_tx):
            return False
        if not _tx_included(clear_tx):
            return False

        attempt = _obj(entry.get("attempt"))
        if not _attempt_rejected_and_included(attempt):
            return False
        if "a8 injected cw20 transfer failure" not in str(attempt.get("raw_log", "")).lower():
            return False
        tx_hashes.add(str(attempt.get("tx_hash")))
        for receipt in (setup_tx, attempt, clear_tx):
            tx_hash = str(receipt.get("tx_hash"))
            if tx_hash in all_hashes:
                return False
            all_hashes.add(tx_hash)

        before = _obj(entry.get("before"))
        after = _obj(entry.get("after"))
        if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
            return False
        if before != after:
            return False
        fault_cw20 = _int_map(before.get("cw20"))
        if (
            before.get("state") != settled_state
            or fault_cw20 is None
            or any(fault_cw20.get(role) != before_cw20.get(role)
                   for role in (*payout_order, "deal"))
            or any(_int_map(before.get(group)) is None
                   for group in ("cw20", "bank_ngonka"))
        ):
            return False

        pending_before = entry.get("pending_before")
        pending_after = entry.get("pending_after")
        if not isinstance(pending_before, (dict, list)) or not pending_before:
            return False
        if not isinstance(pending_after, (dict, list)) or not pending_after:
            return False
        if pending_before != pending_after:
            return False
        if pending_before != payments:
            return False

    # One separate, included receipt per requested recipient-selected target.
    return (
        positions == set(required_positions)
        and len(recipients) == len(required_positions)
        and len(roles) == len(required_positions)
        and len(tx_hashes) == len(required_positions)
        and _validate_r6_delivery(phase, accounts, contracts, expected_amounts, all_hashes)
    )


def _validate_r6_delivery(
    phase: Mapping[str, Any], accounts: Mapping[str, Any],
    contracts: Mapping[str, Any], amounts: Mapping[str, int], prior_hashes: Set[str],
) -> bool:
    """Selected failed withdrawals precede three real payouts and a rejected repeat."""
    claim_tx = _obj(phase.get("claim_tx"))
    if not _tx_included(claim_tx) or claim_tx.get("tx_hash") in prior_hashes:
        return False
    hashes = set(prior_hashes)
    hashes.add(str(claim_tx["tx_hash"]))
    withdrawals = _obj(phase.get("withdrawal_txs"))
    if set(withdrawals) != {"host", "fee", "buyer"}:
        return False
    last_height = max(
        (_as_int(_obj(entry.get("clear_tx")).get("height")) or 0
         for entry in phase["cw20_fault_rollbacks"]),
        default=_as_int(_obj(phase.get("settle_tx")).get("height")) or 0,
    )
    for key, role, event_type in (
        ("host", "host", "wasm-usdt_paid"),
        ("fee", "fee_recipient", "wasm-usdt_paid"),
        ("buyer", "buyer", "wasm-usdt_refunded"),
    ):
        tx = _obj(withdrawals.get(key))
        event = _event_attributes(tx, event_type)
        transfer = _cw20_transfer_attributes(tx, contracts["cw20"])
        tx_height = _as_int(tx.get("height"))
        if (
            not _tx_included(tx) or tx.get("tx_hash") in hashes
            or tx_height is None or tx_height < last_height
            or not event or not transfer
            or event.get("_contract_address") != contracts["deal"]
            or event.get("recipient") != accounts[role]
            or _as_int(event.get("amount_micro_usdt")) != amounts[role]
            or transfer.get("_contract_address") != contracts["cw20"]
            or transfer.get("action") != "transfer"
            or transfer.get("from") != contracts["deal"]
            or transfer.get("to") != accounts[role]
            or _as_int(transfer.get("amount")) != amounts[role]
        ):
            return False
        hashes.add(str(tx["tx_hash"]))
        last_height = tx_height

    repeat = _obj(phase.get("settle_repeat"))
    attempt = _obj(repeat.get("attempt"))
    proof = _obj(repeat.get("proof"))
    return (
        _attempt_rejected_and_included(attempt)
        and attempt.get("tx_hash") not in hashes
        and (_as_int(attempt.get("height")) or 0) >= last_height
        and "cannot settle claim in state" in str(attempt.get("raw_log", "")).lower()
        and proof.get("contract_error") == "InvalidSettlementState"
        and proof.get("layer") == "deliver_tx"
        and proof.get("codespace") == "wasm"
        and proof.get("tx_hash") == attempt.get("tx_hash")
        and _as_int(proof.get("height")) == _as_int(attempt.get("height"))
        and proof.get("matched_contract_error") == "cannot settle claim in state"
    )


def _validate_mainnet_usdt_settlement(scope: Mapping[str, Any]) -> bool:
    """Verify real-token payouts without crediting test-only transfer fault proof."""
    phases = _scope_phases(scope, "claim_settle")
    if len(phases) != 1:
        return False
    phase = phases[0]
    accounts, contracts, terms = (_obj(scope.get(key)) for key in ("accounts", "contracts", "terms"))
    summary = _obj(_obj(phase.get("summary")).get("epochPerformanceSummary"))
    work, reward = _as_int(summary.get("earned_coins", 0)), _as_int(summary.get("rewarded_coins", 0))
    price, budget, fee_bps = (_as_int(terms.get(key)) for key in ("price_micro_usdt_per_gnk", "budget_micro_usdt", "fee_bps"))
    if (
        work is None or reward is None or not 0 <= work < 2**64 or not 0 <= reward < 2**64
        or work + reward <= 0 or price is None or price <= 0 or budget is None or budget <= 0
        or fee_bps != 150 or summary.get("claimed") is not True
        or summary.get("participant_id") != accounts.get("host")
        or _as_int(summary.get("epoch_index")) != _as_int(terms.get("target_epoch"))
        or not _tx_included(_obj(phase.get("settle_tx")))
        or phase.get("cw20_fault_rollbacks") != []
    ):
        return False
    total = work + reward
    capacity = budget * 10**9 // price
    buyer_entitlement = min(total, capacity)
    gross = buyer_entitlement * price // 10**9
    fee = gross * fee_bps // 10000
    amounts = {"host": gross - fee, "fee_recipient": fee, "buyer": budget - gross}
    if any(amount <= 0 for amount in amounts.values()):
        return False
    oracle = {
        "status": "releasing", "work_ngonka": work, "reward_ngonka": reward,
        "total_claim_ngonka": total, "buyer_entitlement_ngonka": buyer_entitlement,
        "host_entitlement_ngonka": total - buyer_entitlement,
        "gnk_release_policy": {"proportional": {"buyer_share_numerator": buyer_entitlement, "share_denominator": total}},
        "gross_usdt": gross, "fee_usdt": fee, "host_net_usdt": gross - fee,
        "buyer_refund_usdt": budget - gross,
    }
    state = _obj(_obj(phase.get("after")).get("deal_state"))
    before = _obj(phase.get("before"))
    expected, actual = _obj(phase.get("expected")), _obj(phase.get("actual"))
    pre_balances, post_balances = _int_map(before.get("cw20")), _int_map(_obj(phase.get("after")).get("cw20"))
    config = _obj(phase.get("deal_config"))
    if (
        not pre_balances or not post_balances or post_balances.get("deal") != 0
        or pre_balances.get("deal") != budget
        or not {"host", "fee_recipient", "buyer", "deal"} <= set(pre_balances)
        or not {"host", "fee_recipient", "buyer", "deal"} <= set(post_balances)
        or _obj(before.get("deal_state")).get("status") != "locked"
        or _obj(before.get("deal_state")).get("buyer") != accounts.get("buyer")
        or _obj(before.get("deal_state")).get("recipient_locked") is not True
        or config.get("host") != accounts.get("host") or config.get("deal_address") != contracts.get("deal")
        or config.get("settlement_cw20") != contracts.get("cw20")
        or config.get("fee_recipient") != accounts.get("fee_recipient")
        or _as_int(config.get("target_epoch")) != _as_int(terms.get("target_epoch"))
        or _as_int(config.get("buyer_budget_micro_usdt")) != budget
        or _as_int(config.get("price_micro_usdt_per_gnk")) != price
        or _as_int(config.get("funded_capacity_ngonka")) != capacity or _as_int(config.get("fee_bps")) != fee_bps
        or _int_map(expected.get("cw20_deltas")) != amounts
        or _int_map(actual.get("cw20_deltas")) != amounts
        or _as_int(expected.get("deal_outflow")) != budget or _as_int(actual.get("deal_outflow")) != budget
        or any(_normalized(state.get(key)) != _normalized(value) or _normalized(expected.get(key)) != _normalized(value)
               for key, value in oracle.items())
        or any(post_balances.get(role, -1) - pre_balances.get(role, -1) != amount for role, amount in amounts.items())
    ):
        return False
    for key, role in (("host", "host"), ("fee", "fee_recipient"), ("buyer", "buyer")):
        payment = _obj(_obj(phase.get("settlement_payments")).get(key))
        if (payment.get("recipient") != accounts.get(role) or _as_int(payment.get("pending_micro_usdt")) != amounts[role]
            or _as_int(payment.get("paid_micro_usdt")) != 0 or _as_int(payment.get("accrued_micro_usdt")) != amounts[role]):
            return False
    event = _event_attributes(_obj(phase.get("settle_tx")), "wasm-claim_settled")
    if (
        not event or event.get("_contract_address") != contracts.get("deal")
        or event.get("host") != accounts.get("host") or event.get("buyer") != accounts.get("buyer")
        or any(_as_int(event.get(key)) != oracle[key] for key in ("gross_usdt", "fee_usdt", "host_net_usdt", "buyer_refund_usdt"))
        or not _validate_r6_delivery(phase, accounts, contracts, amounts, {str(phase["settle_tx"]["tx_hash"])})
    ):
        return False
    repeats = phase.get("withdrawal_repeats")
    if not isinstance(repeats, list) or len(repeats) != 3:
        return False
    hashes = {str(tx["tx_hash"]) for tx in phase["withdrawal_txs"].values()}
    hashes.update((str(phase["claim_tx"]["tx_hash"]), str(phase["settle_tx"]["tx_hash"]), str(phase["settle_repeat"]["attempt"]["tx_hash"])))
    for entry, key in zip(repeats, ("host", "fee", "buyer")):
        before = _obj(entry.get("before"))
        attempt = _obj(entry.get("attempt"))
        if (
            entry.get("role") != key or not _rejected(attempt, "no usdt remains payable for this role")
            or before != entry.get("after") or before.get("state") != state
            or attempt.get("tx_hash") in hashes
            or (_as_int(attempt.get("height")) or 0) < (_as_int(phase["settle_repeat"]["attempt"].get("height")) or 0)
            or any(_as_int(_obj(before.get("cw20")).get(role)) != post_balances.get(role) for role in (*amounts, "deal"))
        ):
            return False
        for payment_key, role in (("host", "host"), ("fee", "fee_recipient"), ("buyer", "buyer")):
            payment = _obj(_obj(before.get("payments")).get(payment_key))
            if (payment.get("recipient") != accounts.get(role) or _as_int(payment.get("paid_micro_usdt")) != amounts[role]
                or _as_int(payment.get("pending_micro_usdt")) != 0 or _as_int(payment.get("accrued_micro_usdt")) != amounts[role]):
                return False
        hashes.add(str(attempt["tx_hash"]))
    return True


def _validate_funded_claim_path(scope: Mapping[str, Any]) -> bool:
    """Bind the resumed native claim, selected fault and final delivery to one Deal.

    Phase names are metadata, not evidence. The funded Testermint method writes
    these four records around its restart and one fee-recipient CW20 rejection.
    """
    phases = scope.get("phases")
    if not isinstance(phases, list):
        return False
    named = {}
    for name in ("claim_prepared", "usdt_fault_rollback", "settlement_delivery_verified", "claim_settle"):
        matches = [(index, phase) for index, phase in enumerate(phases)
                   if isinstance(phase, dict) and phase.get("name") == name]
        if len(matches) != 1:
            return False
        named[name] = matches[0]
    prepared_index, prepared = named["claim_prepared"]
    fault_index, fault_phase = named["usdt_fault_rollback"]
    delivery_index, delivery = named["settlement_delivery_verified"]
    settled_index, settled = named["claim_settle"]
    if not prepared_index < fault_index < delivery_index < settled_index:
        return False

    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))
    terms = _obj(scope.get("terms"))
    config = _obj(settled.get("deal_config"))
    summary = _obj(_obj(settled.get("summary")).get("epochPerformanceSummary"))
    prepared_before = _obj(prepared.get("before"))
    settled_before = _obj(settled.get("before"))
    claim_tx = _obj(settled.get("claim_tx"))
    settle_tx = _obj(settled.get("settle_tx"))
    if (
        not _tx_included(claim_tx) or not _tx_included(settle_tx)
        or _as_int(claim_tx.get("height")) > _as_int(settle_tx.get("height"))
        or claim_tx.get("tx_hash") == settle_tx.get("tx_hash")
        or prepared.get("claim_tx") != claim_tx
        or prepared.get("summary") != settled.get("summary")
        or prepared.get("deal_config") != config
        or _obj(prepared_before.get("deal_state")) != _obj(settled_before.get("deal_state"))
        or not _int_map(prepared_before.get("cw20"))
        or prepared_before.get("cw20") != settled_before.get("cw20")
        or _obj(settled_before.get("deal_state")).get("status") != "locked"
        or _obj(settled_before.get("deal_state")).get("buyer") != accounts.get("buyer")
        or _obj(settled_before.get("deal_state")).get("recipient_locked") is not True
        or summary.get("claimed") is not True
        or summary.get("participant_id") != accounts.get("host")
        or _as_int(summary.get("epoch_index")) != _as_int(terms.get("target_epoch"))
        or config.get("host") != accounts.get("host")
        or config.get("deal_address") != contracts.get("deal")
        or config.get("settlement_cw20") != contracts.get("cw20")
        or config.get("fee_recipient") != accounts.get("fee_recipient")
        or _as_int(config.get("target_epoch")) != _as_int(terms.get("target_epoch"))
        or _as_int(_obj(settled_before.get("cw20")).get("deal"))
        != _as_int(terms.get("budget_micro_usdt"))
        or not _validate_cw20_fault_rollbacks(
            settled.get("cw20_fault_rollbacks"), settled, scope, (2,)
        )
        or fault_phase.get("evidence") != settled["cw20_fault_rollbacks"][0]
    ):
        return False

    expected = _obj(settled.get("expected"))
    amounts = _int_map(expected.get("cw20_deltas"))
    payments = _obj(delivery.get("payments"))
    if not amounts or set(amounts) != {"host", "fee_recipient", "buyer"}:
        return False
    for payment_key, role in (("host", "host"), ("fee", "fee_recipient"), ("buyer", "buyer")):
        payment = _obj(payments.get(payment_key))
        if (
            payment.get("recipient") != accounts.get(role)
            or _as_int(payment.get("accrued_micro_usdt")) != amounts[role]
            or _as_int(payment.get("paid_micro_usdt")) != amounts[role]
            or _as_int(payment.get("pending_micro_usdt")) != 0
        ):
            return False
    return (
        delivery.get("deal") == contracts.get("deal")
        and delivery.get("settle_tx") == settle_tx
        and delivery.get("expected") == expected
        and delivery.get("actual") == {
            "cw20_deltas": _obj(settled.get("actual")).get("cw20_deltas"),
            "deal_outflow": _obj(settled.get("actual")).get("deal_outflow"),
        }
        and delivery.get("state") == _obj(_obj(settled.get("after")).get("deal_state"))
    )


def _cw20_transfer_attributes(tx: Mapping[str, Any], cw20: str) -> Optional[Dict[str, str]]:
    """Find exactly one transfer from this CW20 while allowing unrelated wasm events."""
    events = tx.get("events")
    if not isinstance(events, list):
        return None
    transfers: List[Dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "wasm":
            continue
        attributes = event.get("attributes")
        if not isinstance(attributes, list):
            return None
        values: Dict[str, str] = {}
        for attribute in attributes:
            if not isinstance(attribute, dict):
                return None
            key = attribute.get("key")
            if not isinstance(key, str) or key in values:
                return None
            values[key] = str(attribute.get("value"))
        if values.get("_contract_address") == cw20 and values.get("action") == "transfer":
            transfers.append(values)
    return transfers[0] if len(transfers) == 1 else None


def _normalized(value: Any) -> Any:
    """Canonicalises producer JSON so that "10" and 10 compare equal.

    scripts/acceptance_harness.py mixes ints and numeric strings depending on which
    CLI produced a field, so byte-equality of raw JSON is not a usable
    invariant check. Ordering of mapping keys is normalised as well.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {str(k): _normalized(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    as_int = _as_int(value)
    return as_int if as_int is not None else value


def _int_map(value: Any) -> Optional[Dict[str, int]]:
    """Coerce a producer balance map; on-chain balances cannot be negative."""
    if not isinstance(value, dict) or not value:
        return None
    out: Dict[str, int] = {}
    for key, raw in value.items():
        amount = _as_int(raw)
        if amount is None or amount < 0:
            return None
        out[str(key)] = amount
    return out


def _sections_equal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    sections: Sequence[str],
) -> bool:
    """Every listed section must exist on both sides and be equal.

    A section missing from both snapshots must never compare equal: that is
    exactly how a deleted snapshot would otherwise prove "nothing changed".
    """
    for section in sections:
        if section not in left or section not in right:
            return False
        if _normalized(left[section]) != _normalized(right[section]):
            return False
    return True


def _attempt_rejected(attempt: Any) -> bool:
    """Require an included contract rejection, not a CheckTx or local failure."""
    return _attempt_rejected_and_included(attempt)


def _validate_factory_isolation(phase: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Bind Factory query evidence to every Deal and an included duplicate rejection."""
    if phase.get("level") != "live_network":
        return False
    scenarios = context.get("scenarios")
    if not isinstance(scenarios, dict):
        return False
    records = {"primary": context, **scenarios}
    if not {"routing-mismatch", "routing-missing"} <= records.keys():
        return False
    indexes = phase.get("indexes")
    addresses = phase.get("unique_addresses")
    before = phase.get("deal_snapshot_before")
    after = phase.get("deal_snapshot_after")
    if not all(isinstance(item, dict) and set(item) == set(records)
               for item in (indexes, addresses, before, after)):
        return False
    if (before != after or not all(_nonempty_str(value) for value in addresses.values())
            or len(set(addresses.values())) != len(records)):
        return False
    if not _attempt_rejected(phase.get("duplicate_attempt")):
        return False
    listed_before = phase.get("list_before")
    if not isinstance(listed_before, dict) or phase.get("list_after") != listed_before:
        return False
    listed_deals = listed_before.get("deals")
    if not isinstance(listed_deals, list):
        return False
    listed_addresses = {
        row.get("address") for row in listed_deals if isinstance(row, dict)
        and _nonempty_str(row.get("address"))
    }
    for name, record in records.items():
        if not isinstance(record, dict):
            return False
        host = _obj(record.get("accounts")).get("host")
        epoch = _as_int(_obj(record.get("terms")).get("target_epoch"))
        deal = _obj(record.get("contracts")).get("deal")
        index = indexes[name]
        snapshot = before[name]
        if (
            not _nonempty_str(host) or epoch is None or epoch < 0
            or not _nonempty_str(deal) or addresses[name] != deal
            or deal not in listed_addresses or not isinstance(index, dict)
            or index.get("host") != host or _as_int(index.get("epoch")) != epoch
            or _obj(index.get("row")).get("address") != deal
            or not isinstance(snapshot, dict) or not isinstance(snapshot.get("state"), dict)
            or not snapshot["state"]
            or any(not isinstance(snapshot.get(key), int) or isinstance(snapshot.get(key), bool)
                   or snapshot[key] < 0 for key in ("cw20", "bank_ngonka"))
        ):
            return False
    return True


def _normalized_release_policy(policy: Any) -> Optional[str]:
    """Mirror of acceptance_harness.normalize_release_policy for the two shapes used."""
    if isinstance(policy, str) and policy.strip():
        return policy.strip()
    if isinstance(policy, dict) and len(policy) == 1:
        return str(next(iter(policy)))
    return None


def _vesting_epoch_amounts(response: Any) -> Optional[List[Dict[str, int]]]:
    """Mirror of acceptance_harness.vesting_epoch_amounts; None marks malformed input."""
    if not isinstance(response, dict):
        return None
    schedule = response.get("vesting_schedule")
    if schedule is None:
        return []
    if not isinstance(schedule, dict):
        return None
    epoch_amounts = schedule.get("epoch_amounts")
    if epoch_amounts is None:
        return []
    if not isinstance(epoch_amounts, list):
        return None
    normalized: List[Dict[str, int]] = []
    for epoch in epoch_amounts:
        if not isinstance(epoch, dict):
            return None
        coins = epoch.get("coins")
        if coins is None:
            coins = []
        if not isinstance(coins, list):
            return None
        amounts: Dict[str, int] = {}
        for coin in coins:
            if not isinstance(coin, dict) or not isinstance(coin.get("denom"), str):
                return None
            amount = _as_int(coin.get("amount"))
            if amount is None or amount < 0:
                return None
            denom = coin["denom"]
            amounts[denom] = amounts.get(denom, 0) + amount
        normalized.append(amounts)
    return normalized


def _expected_release(
    before_state: Mapping[str, Any],
    available: int,
) -> Optional[Dict[str, int]]:
    """Independent re-implementation of acceptance_harness.expected_release.

    The verifier must recompute the payout oracle instead of trusting the
    numbers the producer stored, otherwise a rewritten 'final' checkpoint would
    validate itself.
    """
    if available < 0:
        return None
    previous_total = _as_int(before_state.get("released_total_ngonka"))
    previous_buyer = _as_int(before_state.get("buyer_released_ngonka"))
    previous_host = _as_int(before_state.get("host_released_ngonka"))
    if previous_total is None or previous_buyer is None or previous_host is None:
        return None
    if previous_total < 0 or previous_buyer < 0 or previous_host < 0:
        return None

    policy = before_state.get("gnk_release_policy")
    if policy == "host_only":
        numerator, denominator = 0, 1
    elif (
        isinstance(policy, dict)
        and set(policy) == {"proportional"}
        and isinstance(policy["proportional"], dict)
        and set(policy["proportional"]) == {"buyer_share_numerator", "share_denominator"}
    ):
        proportional = policy["proportional"]
        numerator = _as_int(proportional.get("buyer_share_numerator"))
        denominator = _as_int(proportional.get("share_denominator"))
        if numerator is None or denominator is None:
            return None
        if denominator <= 0 or numerator < 0 or numerator > denominator:
            return None
    else:
        return None

    canonical_buyer = previous_total * numerator // denominator
    if (previous_buyer, previous_host) != (canonical_buyer, previous_total - canonical_buyer):
        return None

    released_total = previous_total + available
    buyer_released = released_total * numerator // denominator
    return {
        "released_total": released_total,
        "buyer_released": buyer_released,
        "host_released": released_total - buyer_released,
    }


def _scope_phases(scope: Mapping[str, Any], name: str) -> List[Dict[str, Any]]:
    """All sibling phases of the given name inside the owning scope."""
    return [
        phase
        for phase in (scope.get("phases") or [])
        if isinstance(phase, dict) and phase.get("name") == name
    ]


def _routed_to_deal(recipient_query: Any, epoch: Optional[int], deal: Any) -> bool:
    """The native claim recipient list must route exactly (E, Deal)."""
    if epoch is None or not _nonempty_str(deal):
        return False
    entries = _obj(recipient_query).get("entries")
    if not isinstance(entries, list) or not entries:
        return False
    return any(
        isinstance(item, dict)
        and _as_int(item.get("epoch")) == epoch
        and item.get("recipient") == deal
        for item in entries
    )


def _validate_native_summary_absent(
    phase: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> bool:
    """Validates native_summary_absent at its exact declared offset.

    A bare NotFound proves nothing on its own: the observation must sit on the
    exact target_epoch + expected_offset boundary of the owning scenario, the
    transport must be demonstrably alive, and the Deal must still be the routed
    recipient for that epoch.
    """
    terms = _obj(scope.get("terms"))
    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))

    target = _as_int(terms.get("target_epoch"))
    offset = _as_int(phase.get("expected_offset"))
    phase_target = _as_int(phase.get("target_epoch"))
    observation_epoch = _as_int(_obj(phase.get("observation")).get("epoch"))
    if target is None or offset is None or phase_target is None or observation_epoch is None:
        return False
    if phase_target != target or observation_epoch != target + offset:
        return False
    if phase.get("level") != "live_network":
        return False

    host = accounts.get("host")
    if not _nonempty_str(host) or phase.get("host") != host:
        return False

    native_query = _obj(phase.get("native_query"))
    returncode = _as_int(native_query.get("returncode"))
    if returncode is None or returncode == 0:
        return False
    if native_query.get("grpc_code") != "NotFound":
        return False
    if native_query.get("grpc_description") != "not found":
        return False
    if native_query.get("exact_native_handler_not_found") is not True:
        return False
    diagnostic = "{0}\n{1}".format(
        native_query.get("stdout", ""), native_query.get("stderr", "")
    ).lower()
    if "code = notfound" not in diagnostic or "desc = not found" not in diagnostic:
        return False

    expected = _obj(phase.get("expected"))
    if expected.get("summary_absent") is not True:
        return False
    if expected.get("transport_available") is not True:
        return False
    if expected.get("exact_recipient") is not True:
        return False

    return _routed_to_deal(phase.get("recipient_query"), target, contracts.get("deal"))


def _absence_proof_at(scope: Mapping[str, Any], offset: int, epoch: int) -> bool:
    """The producer requires a same-epoch native NotFound sibling proof."""
    return any(
        _as_int(sibling.get("expected_offset")) == offset
        and _as_int(_obj(sibling.get("observation")).get("epoch")) == epoch
        and _validate_native_summary_absent(sibling, scope)
        for sibling in _scope_phases(scope, "native_summary_absent")
    )


def _validate_native_unclaimed_precondition(
    phase: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> Set[str]:
    """Validates native_unclaimed_precondition against the owning scenario.

    The authoritative summary must belong to this scenario's Host and target
    epoch, the recorded total must be reproducible from the summary itself, and
    the exact Deal recipient routing must still be present.
    """
    checkpoints: Set[str] = set()
    terms = _obj(scope.get("terms"))
    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))

    target = _as_int(terms.get("target_epoch"))
    phase_target = _as_int(phase.get("target_epoch"))
    if target is None or phase_target is None or phase_target != target:
        return checkpoints
    if phase.get("level") != "live_network":
        return checkpoints
    if phase.get("claimed") is not False:
        return checkpoints
    if phase.get("identity_valid") is not True or phase.get("exact_recipient") is not True:
        return checkpoints
    if _as_int(_obj(phase.get("observation")).get("epoch")) is None:
        return checkpoints

    summary = _obj(_obj(phase.get("summary")).get("epochPerformanceSummary"))
    if not summary:
        return checkpoints
    host = accounts.get("host")
    if not _nonempty_str(host) or summary.get("participant_id") != host:
        return checkpoints
    if _as_int(summary.get("epoch_index")) != target:
        return checkpoints

    # The chain's own answer outranks the phase's self-declared flag: the
    # producer aborts on protobuf_bool(summary, "claimed") being true, so an
    # unreadable or true value must never grant an unclaimed precondition.
    if _protobuf_bool(summary, "claimed") is not False:
        return checkpoints

    earned = _as_int(summary.get("earned_coins", 0))
    rewarded = _as_int(summary.get("rewarded_coins", 0))
    total = _as_int(phase.get("total_ngonka"))
    if earned is None or rewarded is None or total is None:
        return checkpoints
    if total != earned + rewarded:
        return checkpoints

    if not _routed_to_deal(phase.get("recipient_query"), target, contracts.get("deal")):
        return checkpoints

    if phase.get("zero_total_required") is True and total == 0:
        checkpoints.add("zero_unclaimed_genesis")
    if phase.get("positive_total_required") is True and total > 0:
        checkpoints.add("positive_unclaimed_precondition")
    return checkpoints


def _unclaimed_precondition_at(
    scope: Mapping[str, Any],
    epoch: Optional[int] = None,
) -> bool:
    """A validated claimed=false precondition exists (optionally at one epoch)."""
    for sibling in _scope_phases(scope, "native_unclaimed_precondition"):
        if not _validate_native_unclaimed_precondition(sibling, scope):
            continue
        if epoch is None:
            return True
        if _as_int(_obj(sibling.get("observation")).get("epoch")) == epoch:
            return True
    return False


# Exact contract markers acceptance_harness.assert_expected_refund_failure demands.
_REFUND_REJECTION_MARKERS: Dict[str, Tuple[str, ...]] = {
    "too_early": (
        "claim-expiry window has not opened",
        "refund window has not opened",
    ),
    "network_unconfirmed_too_early": ("gonka query failed for",),
}


def _validate_refund_rejected(
    phase: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> Set[str]:
    """Validates refund_rejected on the exact boundary it claims to prove.

    A missing attempt, a rejection recorded outside DeliverTx, an unbound epoch
    bracket, a different contract error or any state/balance movement must all
    fail. The awarded checkpoint names an exact epoch offset, so the observed
    epoch is compared against the owning scenario's target epoch.
    """
    checkpoints: Set[str] = set()
    terms = _obj(scope.get("terms"))
    accounts = _obj(scope.get("accounts"))

    target = _as_int(terms.get("target_epoch"))
    reason = phase.get("expected_reason")
    if target is None or reason not in _REFUND_REJECTION_MARKERS:
        return checkpoints

    observed_epoch = _as_int(phase.get("observed_epoch"))
    if observed_epoch is None:
        return checkpoints

    attempt = _obj(phase.get("attempt"))
    if not _attempt_rejected_and_included(attempt):
        return checkpoints
    if not _bracket_binds_tx(phase.get("epoch_bracket"), observed_epoch, attempt):
        return checkpoints

    raw_log = str(attempt.get("raw_log", "")).lower()
    if "out of gas" in raw_log:
        return checkpoints

    actual = _obj(phase.get("actual_reason"))
    if actual.get("reason") != reason or actual.get("layer") != "deliver_tx":
        return checkpoints
    if not _nonempty_str(actual.get("state_status")):
        return checkpoints
    marker = actual.get("matched_contract_error")
    if not _nonempty_str(marker) or str(marker).lower() not in raw_log:
        return checkpoints
    if not any(known in raw_log for known in _REFUND_REJECTION_MARKERS[reason]):
        return checkpoints

    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
        return checkpoints
    if _normalized(before) != _normalized(after):
        return checkpoints

    if reason == "too_early":
        # D1 and the explicit no-Buyer expiry case both require the same
        # positive/zero unclaimed proof.  A missing account field is not
        # treated as Buyer absence: the producer must record ``buyer: null``.
        buyer_is_explicit = "buyer" in accounts and (
            accounts.get("buyer") is None or _nonempty_str(accounts.get("buyer"))
        )
        if (
            buyer_is_explicit
            and observed_epoch == target + 1
            and _unclaimed_precondition_at(scope)
        ):
            checkpoints.add("early_refund_rejected_at_e_plus_1")
    elif reason == "network_unconfirmed_too_early":
        # E1: the producer enforces exactly E+2 plus a same-epoch NotFound proof.
        if observed_epoch == target + 2 and _absence_proof_at(scope, 2, observed_epoch):
            checkpoints.add("emergency_refund_rejected_at_e_plus_2")
    return checkpoints


def _validate_refund_committed(
    phase: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> Set[str]:
    """Validates refund_committed: inclusion, boundary, payouts and rollback.

    A late but still allowed refund must not be credited with proving the exact
    boundary, so the observed epoch is compared for equality and the boundary
    evidence (unclaimed summary / native NotFound) must exist in the same epoch.
    """
    checkpoints: Set[str] = set()
    terms = _obj(scope.get("terms"))
    accounts = _obj(scope.get("accounts"))

    target = _as_int(terms.get("target_epoch"))
    budget = _as_int(terms.get("budget_micro_usdt"))
    reason = phase.get("reason")
    if target is None or budget is None or budget <= 0:
        return checkpoints
    if reason not in (
        "claim_expiry", "network_unconfirmed", "routing_missing", "routing_mismatch"
    ):
        return checkpoints

    observed_epoch = _as_int(phase.get("observed_epoch"))
    tx = _obj(phase.get("tx"))
    if observed_epoch is None or not _tx_included(tx):
        return checkpoints
    if not _bracket_binds_tx(phase.get("epoch_bracket"), observed_epoch, tx):
        return checkpoints

    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    if not _snapshot_sections_present(before) or not _snapshot_sections_present(after):
        return checkpoints

    before_state = _obj(before.get("state"))
    after_state = _obj(after.get("state"))
    buyer = accounts.get("buyer")
    host = accounts.get("host")
    has_buyer = _nonempty_str(buyer)
    expected_status = "refunded" if has_buyer else "expired"
    if after_state.get("status") != expected_status:
        return checkpoints
    if after_state.get("refund_reason") != reason:
        return checkpoints
    if before_state.get("status") == after_state.get("status"):
        return checkpoints

    expected_refund = budget if has_buyer else 0
    expected = _obj(phase.get("expected"))
    if expected.get("status") != expected_status:
        return checkpoints
    if _as_int(expected.get("buyer_refund")) != expected_refund:
        return checkpoints
    if _as_int(expected.get("host_usdt")) != 0:
        return checkpoints
    if _as_int(expected.get("fee_usdt")) != 0:
        return checkpoints
    if _as_int(expected.get("native_transfers")) != 0:
        return checkpoints

    before_cw20 = _int_map(before.get("cw20"))
    after_cw20 = _int_map(after.get("cw20"))
    if before_cw20 is None or after_cw20 is None:
        return checkpoints
    roles = ("deal", "buyer", "host", "fee_recipient")
    if any(role not in before_cw20 or role not in after_cw20 for role in roles):
        return checkpoints
    if after_cw20["buyer"] - before_cw20["buyer"] != expected_refund:
        return checkpoints
    if before_cw20["deal"] - after_cw20["deal"] != expected_refund:
        return checkpoints
    if after_cw20["deal"] != 0:
        return checkpoints
    host_expected_delta = expected_refund if has_buyer and host == buyer else 0
    if after_cw20["host"] - before_cw20["host"] != host_expected_delta:
        return checkpoints
    if after_cw20["fee_recipient"] != before_cw20["fee_recipient"]:
        return checkpoints

    before_bank = _int_map(before.get("bank_ngonka"))
    after_bank = _int_map(after.get("bank_ngonka"))
    if before_bank is None or after_bank is None:
        return checkpoints
    for role in roles:
        if role not in before_bank or before_bank.get(role) != after_bank.get(role):
            return checkpoints

    # The terminal repeat and the competing settlement must both be rejected.
    if not _attempt_rejected(phase.get("terminal_repeat")):
        return checkpoints
    if not _attempt_rejected(phase.get("competing_settlement")):
        return checkpoints

    if reason in ("routing_missing", "routing_mismatch"):
        fault = _obj(phase.get("cw20_fault_rollback"))
        fault_before = _obj(fault.get("before"))
        fault_after = _obj(fault.get("after"))
        if observed_epoch != target:
            return checkpoints
        if not _attempt_rejected(fault.get("attempt")):
            return checkpoints
        if not _tx_included(_obj(fault.get("setup_tx"))):
            return checkpoints
        if not _tx_included(_obj(fault.get("clear_tx"))):
            return checkpoints
        for section in ("state", "cw20", "bank_ngonka", "foreign_cw20"):
            if fault_before.get(section) != fault_after.get(section):
                return checkpoints
        checkpoints.add("routing_refund_committed")
        checkpoints.add("routing_refund_fault_rollback")
    elif reason == "claim_expiry":
        if observed_epoch == target + 2 and _unclaimed_precondition_at(scope, observed_epoch):
            checkpoints.add("claim_expiry_refund_committed_at_e_plus_2")
    else:
        if _normalized_release_policy(after_state.get("gnk_release_policy")) != "host_only":
            return checkpoints
        if observed_epoch == target + 3 and _absence_proof_at(scope, 3, observed_epoch):
            checkpoints.add("emergency_refund_committed")
    return checkpoints


# Sections that terminal_release_snapshot() always records.
_TERMINAL_IMMUTABLE_SECTIONS: Tuple[str, ...] = (
    "state",
    "config",
    "entitlements",
    "release_status",
    "native_status",
    "vesting_total",
    "vesting_schedule",
    "settlement_cw20",
    "foreign_cw20",
)
_TERMINAL_BANK_ROLES: Tuple[str, ...] = ("host", "buyer", "fee_recipient", "caller", "deal")
_TERMINAL_PRECONDITION_FIELDS: Tuple[str, ...] = (
    "positive_total_claim_ngonka",
    "released_total_ngonka",
    "buyer_original_remaining_ngonka",
    "host_original_remaining_ngonka",
    "liquid_balance_ngonka",
    "remaining_vesting_ngonka",
    "scheduled_vesting_ngonka",
)
_TERMINAL_ZERO_PRECONDITIONS: Tuple[str, ...] = (
    "buyer_original_remaining_ngonka",
    "host_original_remaining_ngonka",
    "liquid_balance_ngonka",
    "remaining_vesting_ngonka",
    "scheduled_vesting_ngonka",
)
_TERMINAL_NOTHING_TO_RELEASE_MARKER = "no additional gnk is currently available for release"


def _terminal_snapshot_complete(snapshot: Any) -> bool:
    """Both terminal snapshots must carry the full producer schema."""
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    for section in _TERMINAL_IMMUTABLE_SECTIONS + ("bank",):
        value = snapshot.get(section)
        if not isinstance(value, dict):
            return False
        # vesting_schedule legitimately wraps a null schedule for a drained Deal.
        if section != "vesting_schedule" and not value:
            return False
    for group in ("bank", "settlement_cw20", "foreign_cw20"):
        section = _obj(snapshot.get(group))
        if any(role not in section for role in _TERMINAL_BANK_ROLES):
            return False
    return True


def _validate_terminal_release_repeat(phase: Mapping[str, Any]) -> bool:
    """Validates G3 terminal_release_repeat against the full producer schema.

    Deleting both snapshots, emptying invariant_errors' container or dropping
    the preconditions must all fail: two absent sections are not evidence that
    nothing changed.
    """
    if phase.get("status") != "PASS" or phase.get("level") != "live_network":
        return False
    if phase.get("semantic_error"):
        return False
    invariant_errors = phase.get("invariant_errors")
    if not isinstance(invariant_errors, list) or invariant_errors:
        return False

    caller = _obj(phase.get("caller"))
    if not _nonempty_str(caller.get("address")):
        return False
    if caller.get("role") != "independent_fee_payer":
        return False

    preconditions = _obj(phase.get("preconditions"))
    values = {field: _as_int(preconditions.get(field)) for field in _TERMINAL_PRECONDITION_FIELDS}
    if any(value is None for value in values.values()):
        return False
    if values["positive_total_claim_ngonka"] <= 0:
        return False
    if any(values[field] != 0 for field in _TERMINAL_ZERO_PRECONDITIONS):
        return False
    if values["released_total_ngonka"] < values["positive_total_claim_ngonka"]:
        return False

    if _as_int(_obj(phase.get("epoch_before")).get("epoch")) is None:
        return False
    if _as_int(_obj(phase.get("epoch_after")).get("epoch")) is None:
        return False

    attempt = _obj(phase.get("tx"))
    if not _attempt_rejected_and_included(attempt):
        return False
    raw_log = str(attempt.get("raw_log", "")).lower()
    if _TERMINAL_NOTHING_TO_RELEASE_MARKER not in raw_log:
        return False

    rejection = _obj(phase.get("terminal_rejection"))
    if rejection.get("contract_error") != "NothingToRelease":
        return False
    if rejection.get("layer") != "deliver_tx" or rejection.get("codespace") != "wasm":
        return False
    rejection_code = rejection.get("code")
    if not isinstance(rejection_code, int) or isinstance(rejection_code, bool) or rejection_code == 0:
        return False
    rejection_height = _as_int(rejection.get("height"))
    if rejection_height is None or rejection_height <= 0:
        return False
    if rejection.get("tx_hash") != attempt.get("tx_hash"):
        return False
    if rejection_height != _as_int(attempt.get("height")):
        return False
    if str(rejection.get("matched_contract_error", "")).lower() != _TERMINAL_NOTHING_TO_RELEASE_MARKER:
        return False

    expected = _obj(phase.get("expected"))
    if expected.get("deliver_tx_included") is not True:
        return False
    if expected.get("contract_error") != "NothingToRelease":
        return False
    if expected.get("arbitrary_error_accepted") is not False:
        return False
    if expected.get("state_entitlements_and_counters_unchanged") is not True:
        return False
    if expected.get("all_tracked_cw20_balances_unchanged") is not True:
        return False
    if expected.get("only_caller_gnk_fee_may_change") is not True:
        return False
    if _as_int(expected.get("deal_gnk_transfers")) != 0:
        return False
    if _as_int(expected.get("repeat_payout")) != 0:
        return False

    before = phase.get("before")
    after = phase.get("after")
    if not _terminal_snapshot_complete(before) or not _terminal_snapshot_complete(after):
        return False
    if not _sections_equal(before, after, _TERMINAL_IMMUTABLE_SECTIONS):
        return False

    before_bank = _obj(_obj(before).get("bank"))
    after_bank = _obj(_obj(after).get("bank"))
    for role in _TERMINAL_BANK_ROLES:
        if role == "caller":
            continue
        if _normalized(before_bank.get(role)) != _normalized(after_bank.get(role)):
            return False

    caller_before = _int_map(before_bank.get("caller"))
    caller_after = _int_map(after_bank.get("caller"))
    if caller_before is None or caller_after is None:
        return False
    denoms = set(caller_before) | set(caller_after)
    deltas = {
        denom: caller_after.get(denom, 0) - caller_before.get(denom, 0) for denom in denoms
    }
    if any(delta != 0 for denom, delta in deltas.items() if denom != DEFAULT_DENOM):
        return False
    if deltas.get(DEFAULT_DENOM, 0) > 0:
        return False

    recorded_deltas = phase.get("caller_bank_deltas")
    if not isinstance(recorded_deltas, dict):
        return False
    if _normalized(recorded_deltas) != _normalized(deltas):
        return False

    return True


# Sections that r2_gift_snapshot() always records.
_R2_SNAPSHOT_SECTIONS: Tuple[str, ...] = (
    "state",
    "config",
    "entitlements",
    "release_status",
    "native_status",
    "vesting_total",
    "vesting_schedule",
    "bank",
    "settlement_cw20",
)
_R2_IMMUTABLE_SECTIONS: Tuple[str, ...] = ("config", "entitlements", "settlement_cw20")
_R2_SNAPSHOT_ROLES: Tuple[str, ...] = ("host", "buyer", "fee_recipient", "deal")
_R2_MUTABLE_STATE_KEYS = {
    "released_total_ngonka",
    "buyer_released_ngonka",
    "host_released_ngonka",
}
_R2_STAGES: Tuple[str, ...] = ("pre_gift", "fully_locked", "first_unlocked", "final")
# Exactly the roles release_scenario measures around a release transaction.
_RELEASE_BANK_ROLES: Tuple[str, ...] = ("deal", "buyer", "host")
# A release moves the cumulative counters and may finish the Deal. The recorded
# runs show "releasing" -> "releasing", "releasing" -> "completed",
# "completed" -> "completed" and "refunded" -> "refunded" (a refunded Deal still
# pays its GNK out under the host_only policy), so the only status change a
# release may make is the one that closes it.
_RELEASE_COMPLETION: Tuple[str, str] = ("releasing", "completed")
_RELEASE_MUTABLE_STATE_KEYS = set(_R2_MUTABLE_STATE_KEYS) | {"status"}


def _r2_snapshot_complete(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    for section in _R2_SNAPSHOT_SECTIONS:
        value = snapshot.get(section)
        if not isinstance(value, dict):
            return False
        if section != "vesting_schedule" and not value:
            return False
    for group in ("bank", "settlement_cw20"):
        section = _obj(snapshot.get(group))
        if any(role not in section for role in _R2_SNAPSHOT_ROLES):
            return False
    return True


def _r2_native_totals(snapshot: Mapping[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    """Returns (pending, liquid, schedule_total, tranche_count) or None."""
    native = _obj(snapshot.get("native_status"))
    pending = _as_int(native.get("remaining_vesting_ngonka"))
    liquid = _as_int(native.get("liquid_balance_ngonka"))
    schedule = _vesting_epoch_amounts(snapshot.get("vesting_schedule"))
    if pending is None or liquid is None or schedule is None:
        return None
    return pending, liquid, sum(e.get(DEFAULT_DENOM, 0) for e in schedule), len(schedule)


def _bank_ngonka(snapshot: Mapping[str, Any], role: str) -> Optional[int]:
    """GNK the role holds in a r2_gift_snapshot bank section.

    The producer stores an empty mapping for an empty account, so {} is a real
    zero balance while a missing role is missing evidence.
    """
    bank = snapshot.get("bank")
    if not isinstance(bank, dict):
        return None
    balances = bank.get(role)
    if not isinstance(balances, dict):
        return None
    if not balances:
        return 0
    return _as_int(balances.get(DEFAULT_DENOM, 0))


def _vesting_total_ngonka(snapshot: Mapping[str, Any]) -> Optional[int]:
    """Sum of the native vesting total the chain reports for the Deal.

    A completed vesting has no coins left, which the node serialises as null.
    """
    section = snapshot.get("vesting_total")
    if not isinstance(section, dict):
        return None
    coins = section.get("total_amount")
    if coins is None:
        return 0
    if not isinstance(coins, list):
        return None
    total = 0
    for coin in coins:
        if not isinstance(coin, dict) or coin.get("denom") != DEFAULT_DENOM:
            return None
        amount = _as_int(coin.get("amount"))
        if amount is None or amount < 0:
            return None
        total += amount
    return total


def _r2_stage_facts(
    snapshot: Mapping[str, Any],
    baseline: Mapping[str, Any],
    baseline_bank: Mapping[str, int],
) -> Optional[Dict[str, Any]]:
    """Cross-checks one stage's chain views against each other and the baseline.

    Contract state, the native vesting view and the bank balances are three
    independent reads of the same moment; a stage only counts when they agree,
    so a rewritten counter cannot hide behind an untouched balance.
    """
    state = _obj(snapshot.get("state"))
    if state.get("status") != "completed":
        return None
    if not _sections_equal(snapshot, baseline, _R2_IMMUTABLE_SECTIONS):
        return None

    baseline_state = _obj(baseline.get("state"))
    frozen_now = {k: v for k, v in state.items() if k not in _R2_MUTABLE_STATE_KEYS}
    frozen_before = {k: v for k, v in baseline_state.items() if k not in _R2_MUTABLE_STATE_KEYS}
    if not frozen_now or _normalized(frozen_now) != _normalized(frozen_before):
        return None

    totals = _r2_native_totals(snapshot)
    if totals is None:
        return None
    pending, liquid, schedule_total, tranches = totals

    bank: Dict[str, int] = {}
    for role in _R2_SNAPSHOT_ROLES:
        amount = _bank_ngonka(snapshot, role)
        if amount is None or amount < 0:
            return None
        bank[role] = amount

    # The Deal contract earns nothing else on this chain, so every coin it
    # holds is exactly the liquid balance the release will pay out.
    if bank["deal"] != liquid:
        return None
    # The x/streamvesting total must match the module's own remaining amount.
    if _vesting_total_ngonka(snapshot) != pending:
        return None
    # A vested gift is not a settlement: the marketplace fee recipient is never
    # paid again after pre_gift.
    if bank["fee_recipient"] > baseline_bank["fee_recipient"]:
        return None

    released: Dict[str, Optional[int]] = {
        key: _as_int(state.get(key)) for key in _R2_MUTABLE_STATE_KEYS
    }
    if any(value is None or value < 0 for value in released.values()):
        return None
    # release_status is the contract's second view of the same counters.
    status = _obj(snapshot.get("release_status"))
    for key, value in released.items():
        if _as_int(status.get(key)) != value:
            return None

    return {
        "state": state,
        "pending": pending,
        "liquid": liquid,
        "schedule_total": schedule_total,
        "tranches": tranches,
        "bank": bank,
        "released": released,
    }


def _r2_payout_reached(
    before: Mapping[str, int],
    after: Mapping[str, int],
    buyer_paid: int,
    host_paid: int,
) -> bool:
    """Buyer and Host bank GNK must have grown by at least the gift payout."""
    return (
        after["buyer"] - before["buyer"] >= buyer_paid
        and after["host"] - before["host"] >= host_paid
    )


def _event_attributes(tx: Mapping[str, Any], event_type: str) -> Optional[Dict[str, str]]:
    """The attributes of the single event of this type, or None.

    filtered_tx keeps the release events verbatim, so a receipt carries the
    chain's own account of what it paid and to whom.
    """
    events = tx.get("events")
    if not isinstance(events, list):
        return None
    found: Optional[Dict[str, str]] = None
    for event in events:
        if not isinstance(event, dict) or event.get("type") != event_type:
            continue
        if found is not None:
            return None
        attributes = event.get("attributes")
        if not isinstance(attributes, list):
            return None
        collected: Dict[str, str] = {}
        for attribute in attributes:
            if not isinstance(attribute, dict):
                return None
            key = attribute.get("key")
            if not isinstance(key, str) or key in collected:
                return None
            collected[key] = str(attribute.get("value"))
        found = collected
    return found


def _no_bank_transfer_involving(tx: Mapping[str, Any], address: str) -> bool:
    """Require a well-formed event list with no Bank transfer to or from address."""
    events = tx.get("events")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "transfer":
            continue
        attributes = event.get("attributes")
        if not isinstance(attributes, list):
            return False
        observed: Dict[str, str] = {}
        for item in attributes:
            if not isinstance(item, Mapping):
                return False
            key = item.get("key")
            if not isinstance(key, str) or key in observed:
                return False
            observed[key] = str(item.get("value"))
        if not _nonempty_str(observed.get("sender")) or not _nonempty_str(observed.get("recipient")):
            return False
        if address in (observed.get("sender"), observed.get("recipient")):
            return False
    return True


def _coin_amount(value: Any, denom: str) -> Optional[int]:
    """A release BankMsg sends one positive coin of the expected denom."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = re.fullmatch(r"\s*(\d+)([a-zA-Z][a-zA-Z0-9/:._-]*)\s*", value)
    if match is None or match.group(2) != denom:
        return None
    amount = int(match.group(1))
    return amount if amount > 0 else None


def _deal_transfer_totals(tx: Mapping[str, Any], deal: str) -> Optional[Dict[str, int]]:
    """GNK the Deal sent per recipient in this transaction."""
    events = tx.get("events")
    if not isinstance(events, list):
        return None
    totals: Dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "transfer":
            continue
        attributes = event.get("attributes")
        if not isinstance(attributes, list):
            return None
        collected: Dict[str, Any] = {}
        for attribute in attributes:
            if not isinstance(attribute, dict):
                return None
            key = attribute.get("key")
            if not isinstance(key, str) or key in collected:
                return None
            collected[key] = attribute.get("value")
        if collected.get("sender") != deal:
            continue
        recipient = collected.get("recipient")
        amount = _coin_amount(collected.get("amount"), DEFAULT_DENOM)
        if not _nonempty_str(recipient) or amount is None:
            return None
        totals[recipient] = totals.get(recipient, 0) + amount
    return totals


def _release_counters(state: Any) -> Optional[Dict[str, int]]:
    """The three cumulative release counters of a Deal state document."""
    if not isinstance(state, Mapping) or not state:
        return None
    counters: Dict[str, int] = {}
    for key in _R2_MUTABLE_STATE_KEYS:
        value = _as_int(state.get(key))
        if value is None or value < 0:
            return None
        counters[key] = value
    return counters


def _validate_release_receipt(
    phase: Mapping[str, Any],
    strict: bool = False,
) -> Optional[Dict[str, Any]]:
    """Recomputes one release_unlocked_gnk receipt from the evidence it carries.

    Every release is checked against the chain's own account of it: the
    included transaction and its events, the Deal/Buyer/Host balances measured
    around that single transaction and the independently recomputed oracle.

    scripts/acceptance_harness.py now writes one receipt shape from close_gnk_release
    for both `release` and `release-scenario`, but contexts recorded before that
    unification carry no epoch bracket, caller or address_delta. Those extras
    are always validated when present and are mandatory under strict=True,
    which the R2 gift payout chain uses.

    Returns the recomputed facts, or None when anything fails to reproduce.
    """
    tx = _obj(phase.get("tx"))
    if not _tx_included(tx):
        return None
    tx_height = _as_int(tx.get("height"))
    if tx_height is None:
        return None

    epoch: Optional[int] = None
    bracket = _obj(phase.get("epoch_bracket"))
    if bracket:
        if bracket.get("same_epoch") is not True:
            return None
        epoch = _as_int(bracket.get("epoch"))
        before_height = _as_int(bracket.get("before_height"))
        bracket_height = _as_int(bracket.get("tx_height"))
        after_height = _as_int(bracket.get("after_height"))
        if epoch is None or before_height is None or bracket_height is None:
            return None
        if after_height is None or not before_height <= bracket_height <= after_height:
            return None
        # The bracket must describe this very receipt, not a neighbouring one.
        if bracket_height != tx_height:
            return None
    elif strict:
        return None
    # A GNK release may never spend the foreign CW20 the Deal might hold.
    foreign_before = _normalized(phase.get("foreign_cw20_before"))
    if foreign_before != _normalized(phase.get("foreign_cw20_after")):
        return None

    bank_before = _int_map(phase.get("bank_before"))
    bank_after = _int_map(phase.get("bank_after"))
    if bank_before is None or bank_after is None:
        return None
    if set(bank_before) != set(_RELEASE_BANK_ROLES) or set(bank_after) != set(_RELEASE_BANK_ROLES):
        return None
    available = bank_before["deal"]
    if available <= 0 or bank_after["deal"] != 0:
        return None

    state_before = _obj(phase.get("state_before"))
    state_after = _obj(phase.get("state_after"))
    before_counters = _release_counters(state_before)
    after_counters = _release_counters(state_after)
    if before_counters is None or after_counters is None:
        return None
    # A release moves the three cumulative counters and may close the Deal;
    # nothing else in the state document is allowed to change.
    frozen_before = {
        k: v for k, v in state_before.items() if k not in _RELEASE_MUTABLE_STATE_KEYS
    }
    frozen_after = {
        k: v for k, v in state_after.items() if k not in _RELEASE_MUTABLE_STATE_KEYS
    }
    if not frozen_before or _normalized(frozen_before) != _normalized(frozen_after):
        return None
    status_before = state_before.get("status")
    status_after = state_after.get("status")
    if not _nonempty_str(status_before) or not _nonempty_str(status_after):
        return None
    if status_before != status_after and (status_before, status_after) != _RELEASE_COMPLETION:
        return None

    expected = _expected_release(state_before, available)
    if expected is None:
        return None
    if after_counters != {
        "released_total_ngonka": expected["released_total"],
        "buyer_released_ngonka": expected["buyer_released"],
        "host_released_ngonka": expected["host_released"],
    }:
        return None
    buyer_delta = expected["buyer_released"] - before_counters["buyer_released_ngonka"]
    host_delta = expected["host_released"] - before_counters["host_released_ngonka"]
    if buyer_delta < 0 or host_delta < 0 or buyer_delta + host_delta != available:
        return None

    actual = _obj(phase.get("actual"))
    coincident = actual.get("coincident_recipients")
    if coincident is None and not strict:
        # The funded-claim producer refuses to run with Host == Buyer, so its
        # receipts describe two distinct recipients by construction.
        coincident = False
    if coincident is True:
        # release_scenario reads one address twice when Host and Buyer coincide.
        address_delta = bank_after["host"] - bank_before["host"]
    elif coincident is False:
        if bank_after["buyer"] - bank_before["buyer"] != buyer_delta:
            return None
        if bank_after["host"] - bank_before["host"] != host_delta:
            return None
        address_delta = buyer_delta + host_delta
    else:
        return None
    # A release must conserve the Deal's whole spendable balance, which is
    # exactly what the producer asserts before recording anything.
    if address_delta != available:
        return None

    if _as_int(actual.get("buyer_delta")) != buyer_delta:
        return None
    if _as_int(actual.get("host_delta")) != host_delta:
        return None
    if "address_delta" in actual or strict:
        if _as_int(actual.get("address_delta")) != address_delta:
            return None
    # close_gnk_release also mirrors the resulting counters into actual.
    for key, value in (
        ("released_total", expected["released_total"]),
        ("buyer_released", expected["buyer_released"]),
        ("host_released", expected["host_released"]),
    ):
        if key in actual and _as_int(actual.get(key)) != value:
            return None

    caller = phase.get("caller")
    caller_fee_delta = _as_int(phase.get("caller_native_fee_delta"))
    if strict and (caller_fee_delta is None or not _nonempty_str(caller)):
        return None
    if caller is not None and not _nonempty_str(caller):
        return None
    if phase.get("caller_native_fee_delta") is not None and caller_fee_delta is None:
        return None

    recorded = _obj(phase.get("expected"))
    for key, value in (
        ("released_total", expected["released_total"]),
        ("buyer_released", expected["buyer_released"]),
        ("host_released", expected["host_released"]),
        ("buyer_delta", buyer_delta),
        ("host_delta", host_delta),
    ):
        if _as_int(recorded.get(key)) != value:
            return None

    # The transaction's own events say which contract paid, how much and to
    # whom; that is the only part of the record the chain itself authored.
    released = _event_attributes(tx, "wasm-gnk_released")
    if not released or released.get("entry_point") != "release_unlocked_gnk":
        return None
    deal = released.get("deal")
    buyer = released.get("buyer")
    host = released.get("host")
    if not _nonempty_str(deal) or not _nonempty_str(host):
        return None
    buyer_absent = buyer is None
    if buyer_absent:
        # Buyer-less Deals omit the optional Buyer event attribute.  That is
        # only valid when both state snapshots explicitly agree that there is
        # no Buyer and the independently recomputed Buyer payout is zero.
        if (
            state_before.get("buyer") is not None
            or state_after.get("buyer") is not None
            or buyer_delta != 0
        ):
            return None
    elif not _nonempty_str(buyer):
        return None
    if released.get("_contract_address") != deal:
        return None
    for key, value in (
        ("available_balance_ngonka", available),
        ("released_total_ngonka", expected["released_total"]),
        ("buyer_released_ngonka", expected["buyer_released"]),
        ("host_released_ngonka", expected["host_released"]),
        ("buyer_delta_ngonka", buyer_delta),
        ("host_delta_ngonka", host_delta),
    ):
        if _as_int(released.get(key)) != value:
            return None

    transfers = _deal_transfer_totals(tx, deal)
    if transfers is None:
        return None
    payouts: Dict[str, int] = {}
    for recipient, amount in ((buyer, buyer_delta), (host, host_delta)):
        if amount:
            if not _nonempty_str(recipient):
                return None
            payouts[recipient] = payouts.get(recipient, 0) + amount
    # Exactly the recomputed payout left the Deal, and nothing went elsewhere.
    if transfers != payouts:
        return None
    if sum(transfers.values()) != available:
        return None

    return {
        "tx_hash": str(tx.get("tx_hash")),
        "epoch": epoch,
        "height": tx_height,
        "available": available,
        "buyer_delta": buyer_delta,
        "host_delta": host_delta,
        "before": before_counters,
        "after": after_counters,
        "frozen_state": frozen_before,
        "bank_before": bank_before,
        "bank_after": bank_after,
        "caller": caller,
        "caller_fee_delta": caller_fee_delta,
        "deal": deal,
        "buyer": buyer,
        "host": host,
    }


def _validate_late_completed_donation_release(phase: Mapping[str, Any]) -> bool:
    """Reproduce both post-completion donations and cumulative releases."""
    roles = _obj(phase.get("roles"))
    donor = roles.get("donor")
    deal = roles.get("deal")
    host = roles.get("host")
    buyer = roles.get("buyer")
    if not all(_nonempty_str(value) for value in (donor, deal, host, buyer)):
        return False
    if len({donor, deal, host, buyer, roles.get("fee_recipient"), roles.get("caller")}) != 6:
        return False

    releases = phase.get("releases")
    if not isinstance(releases, list) or len(releases) != 2:
        return False
    funding = _obj(phase.get("donor_funding_tx"))
    if not _tx_included(funding):
        return False
    funding_events = _event_attributes(funding, "transfer")
    rounding = _obj(phase.get("rounding"))
    numerator = _as_int(rounding.get("buyer_numerator"))
    denominator = _as_int(rounding.get("denominator"))
    if numerator is None or denominator is None or denominator <= 0 or not 0 < numerator < denominator:
        return False

    first_facts: Optional[Dict[str, Any]] = None
    heights: list[int] = []
    hashes: list[str] = []
    amounts: list[int] = []
    for index, record_value in enumerate(releases):
        record = _obj(record_value)
        expected_label = "first" if index == 0 else "second"
        amount = _as_int(record.get("amount_ngonka"))
        if record.get("label") != expected_label or amount is None or amount <= 0:
            return False
        amounts.append(amount)
        before = _obj(record.get("before"))
        before_release = _obj(record.get("before_release"))
        after = _obj(record.get("after"))
        before_state = _obj(before.get("state"))
        if before_state.get("status") != "completed" or before_state != _obj(before_release.get("state")):
            return False
        release_status = _obj(before.get("release_status"))
        native_status = _obj(before.get("native_status"))
        total_claim = _as_int(before_state.get("total_claim_ngonka"))
        if (
            total_claim is None
            or total_claim <= 0
            or (_as_int(release_status.get("released_total_ngonka")) or 0) < total_claim
            or _as_int(release_status.get("buyer_original_remaining_ngonka")) != 0
            or _as_int(release_status.get("host_original_remaining_ngonka")) != 0
            or _as_int(native_status.get("liquid_balance_ngonka")) != 0
            or _as_int(native_status.get("remaining_vesting_ngonka")) != 0
            or _vesting_total_ngonka(before) != 0
            or _vesting_epoch_amounts(before.get("vesting_schedule")) != []
        ):
            return False
        bank_before = _obj(before.get("bank"))
        bank_funded = _obj(before_release.get("bank"))
        bank_after = _obj(after.get("bank"))
        if any(
            not isinstance(bank.get(role), Mapping)
            for bank in (bank_before, bank_funded, bank_after)
            for role in ("deal", "buyer", "host", "fee_recipient", "caller")
        ):
            return False
        # Cosmos Bank omits zero-balance denoms from a balances response.
        # A missing coin in a recorded balance map means zero; a present but
        # malformed amount still fails integer validation.
        deal_before = _as_int(_obj(bank_before.get("deal")).get(DEFAULT_DENOM, 0))
        deal_funded = _as_int(_obj(bank_funded.get("deal")).get(DEFAULT_DENOM, 0))
        if deal_before != 0 or deal_funded != amount:
            return False
        for role in ("host", "buyer", "fee_recipient", "caller"):
            if _obj(bank_before.get(role)) != _obj(bank_funded.get(role)):
                return False
        # The old Completed state, frozen claims and all CW20/vesting state
        # must survive the Bank send and release unchanged.
        for section in ("config", "entitlements", "settlement_cw20", "foreign_cw20", "vesting_total", "vesting_schedule"):
            if before.get(section) != before_release.get(section) or before.get(section) != (first_facts or {}).get("baseline", before).get(section):
                return False
            if after.get(section) != (first_facts or {}).get("baseline", before).get(section):
                return False
        donation = _obj(record.get("donation_tx"))
        release_tx = _obj(record.get("release_tx"))
        if not _tx_included(donation) or not _tx_included(release_tx):
            return False
        donation_event = _event_attributes(donation, "transfer")
        if (
            donation_event is None
            or donation_event.get("sender") != donor
            or donation_event.get("recipient") != deal
            or _coin_amount(donation_event.get("amount"), DEFAULT_DENOM) != amount
        ):
            return False
        receipt = {
            "tx": release_tx,
            "state_before": _obj(before_release.get("state")),
            "state_after": _obj(after.get("state")),
            "bank_before": {role: _as_int(_obj(bank_funded.get(role)).get(DEFAULT_DENOM, 0)) for role in ("deal", "buyer", "host")},
            "bank_after": {role: _as_int(_obj(bank_after.get(role)).get(DEFAULT_DENOM, 0)) for role in ("deal", "buyer", "host")},
            "foreign_cw20_before": _obj(before_release.get("foreign_cw20")).get("deal"),
            "foreign_cw20_after": _obj(after.get("foreign_cw20")).get("deal"),
            "expected": _obj(record.get("expected")),
            "actual": _obj(record.get("actual")),
        }
        facts = _validate_release_receipt(receipt)
        actual = _obj(record.get("actual"))
        if (
            facts is None
            or facts["deal"] != deal
            or facts["host"] != host
            or facts["buyer"] != buyer
            or _as_int(actual.get("buyer_delta")) != facts["buyer_delta"]
            or _as_int(actual.get("host_delta")) != facts["host_delta"]
            or _as_int(_obj(bank_after.get("fee_recipient")).get(DEFAULT_DENOM))
            != _as_int(_obj(bank_funded.get("fee_recipient")).get(DEFAULT_DENOM))
            or (_as_int(_obj(bank_after.get("caller")).get(DEFAULT_DENOM)) or 0)
            > (_as_int(_obj(bank_funded.get("caller")).get(DEFAULT_DENOM)) or 0)
        ):
            return False
        tx_height = _as_int(donation.get("height"))
        release_height = _as_int(release_tx.get("height"))
        tx_hash = donation.get("tx_hash")
        release_hash = release_tx.get("tx_hash")
        if tx_height is None or release_height is None or not isinstance(tx_hash, str) or not isinstance(release_hash, str):
            return False
        heights.extend((tx_height, release_height))
        hashes.extend((tx_hash, release_hash))
        if index == 0:
            first_facts = dict(facts)
            first_facts["baseline"] = before
        elif (
            first_facts is None
            or facts["height"] <= first_facts["height"]
            or facts["tx_hash"] == first_facts["tx_hash"]
            or _normalized(_obj(before.get("state"))) != _normalized(
                _obj(_obj(releases[0]).get("after")).get("state")
            )
            or any(
                _obj(bank_before.get(role)) != _obj(
                    _obj(_obj(_obj(releases[0]).get("after")).get("bank")).get(role)
                )
                for role in ("host", "buyer", "fee_recipient", "caller")
            )
        ):
            return False

    funding_height = _as_int(funding.get("height"))
    funding_hash = funding.get("tx_hash")
    total_funded = sum(amounts) + 1_000_000
    if (
        funding_events is None
        or not _nonempty_str(funding_events.get("sender"))
        or funding_events.get("sender") == donor
        or funding_events.get("recipient") != donor
        or _coin_amount(funding_events.get("amount"), DEFAULT_DENOM) != total_funded
        or funding_height is None
        or not isinstance(funding_hash, str)
        or funding_height >= heights[0]
        or len(set(hashes + [funding_hash])) != 5
        or not heights[0] < heights[1] < heights[2] < heights[3]
    ):
        return False
    independent = amounts[0] * numerator // denominator
    remainder = amounts[0] * numerator % denominator
    second_actual = _obj(_obj(releases[1]).get("actual"))
    return (
        amounts[0] == amounts[1]
        and independent > 0
        and independent < amounts[0]
        and remainder > 0
        and _as_int(rounding.get("independent_buyer_per_donation")) == independent
        and _as_int(rounding.get("remainder")) == remainder
        and _as_int(second_actual.get("buyer_delta")) != independent
    )


def _validate_funded_release_lifecycle(scope: Mapping[str, Any]) -> bool:
    """Require both bootstrap release attempts and the final GNK outcome.

    The second attempt can pay a newly unlocked tranche or reject with
    NothingToRelease when the first payout already completed the Deal.
    """
    phases = scope.get("phases")
    if not isinstance(phases, list):
        return False
    attempts = [(index, phase) for index, phase in enumerate(phases)
                if isinstance(phase, dict)
                and phase.get("name") in {"release", "release_repeat_rejected"}]
    settlements = [(index, phase) for index, phase in enumerate(phases)
                   if isinstance(phase, dict) and phase.get("name") == "claim_settle"]
    if len(attempts) != 2 or len(settlements) != 1:
        return False
    settle_index, settlement = settlements[0]
    (first_index, first), (second_index, second) = attempts
    if not settle_index < first_index < second_index or first.get("name") != "release":
        return False
    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))
    first_facts = _validate_release_receipt(first, strict=True)
    if (
        first_facts is None
        or first_facts["deal"] != contracts.get("deal")
        or first_facts["host"] != accounts.get("host")
        or first_facts["buyer"] != accounts.get("buyer")
        or first.get("state_before") != _obj(_obj(settlement.get("after")).get("deal_state"))
        or first_facts["before"] != {
            "released_total_ngonka": 0,
            "buyer_released_ngonka": 0,
            "host_released_ngonka": 0,
        }
        or first_facts["height"] <= (_as_int(_obj(settlement.get("settle_tx")).get("height")) or 0)
    ):
        return False

    first_after = _obj(first.get("state_after"))
    if second.get("name") == "release":
        second_facts = _validate_release_receipt(second, strict=True)
        if second_facts is None:
            return False
        # Buyer and Host can receive unrelated native funds between epochs.
        # Each receipt already proves its own before/after payout deltas.
        return (
            first_after.get("status") == "releasing"
            and second.get("state_before") == first_after
            and second_facts["deal"] == first_facts["deal"]
            and second_facts["host"] == first_facts["host"]
            and second_facts["buyer"] == first_facts["buyer"]
            and second_facts["tx_hash"] != first_facts["tx_hash"]
            and second_facts["height"] > first_facts["height"]
            and _obj(second.get("state_after")).get("status") == "completed"
            and _as_int(_obj(second.get("state_after")).get("buyer_released_ngonka"))
            == _as_int(_obj(second.get("state_after")).get("buyer_entitlement_ngonka"))
            and _as_int(_obj(second.get("state_after")).get("host_released_ngonka"))
            == _as_int(_obj(second.get("state_after")).get("host_entitlement_ngonka"))
        )

    if first_after.get("status") != "completed":
        return False
    attempt = _obj(second.get("tx"))
    rejection = _obj(second.get("terminal_rejection"))
    bracket = _obj(second.get("epoch_bracket"))
    expected = _obj(second.get("expected"))
    attempt_height = _as_int(attempt.get("height"))
    if not _attempt_rejected_and_included(attempt) or attempt_height is None:
        return False
    if (
        second.get("deal") != first_facts["deal"]
        or second.get("level") != "live_network"
        or attempt_height <= first_facts["height"]
        or attempt.get("tx_hash") == first_facts["tx_hash"]
        or _TERMINAL_NOTHING_TO_RELEASE_MARKER
        not in str(attempt.get("raw_log", "")).lower()
        or rejection.get("contract_error") != "NothingToRelease"
        or rejection.get("layer") != "deliver_tx"
        or rejection.get("codespace") != "wasm"
        or rejection.get("tx_hash") != attempt.get("tx_hash")
        or _as_int(rejection.get("height")) != attempt_height
        or _as_int(rejection.get("code")) != _as_int(attempt.get("code"))
        or str(rejection.get("matched_contract_error", "")).lower()
        != _TERMINAL_NOTHING_TO_RELEASE_MARKER
        or bracket.get("same_epoch") is not True
        or _as_int(bracket.get("tx_height")) != attempt_height
        or not (_as_int(bracket.get("before_height")) or 0) <= attempt_height
        <= (_as_int(bracket.get("after_height")) or 0)
        or _as_int(bracket.get("epoch")) is None
        or second.get("before_state") != first_after
        or second.get("after_state") != first_after
        or second.get("before") != first_facts["bank_after"]
        or second.get("after") != second.get("before")
        or _as_int(_obj(second.get("before")).get("deal")) != 0
        or second.get("foreign_cw20_before") != second.get("foreign_cw20_after")
        or not _nonempty_str(second.get("caller"))
        or _as_int(second.get("caller_native_fee_delta")) is None
        or _as_int(second.get("caller_native_fee_delta")) > 0
        or expected != {"contract_error": "NothingToRelease",
                        "native_transfers": 0, "state_unchanged": True}
        or _deal_transfer_totals(attempt, first_facts["deal"]) != {}
    ):
        return False
    return (
        _as_int(first_after.get("buyer_released_ngonka"))
        == _as_int(first_after.get("buyer_entitlement_ngonka"))
        and _as_int(first_after.get("host_released_ngonka"))
        == _as_int(first_after.get("host_entitlement_ngonka"))
    )


def _validate_no_sale_vesting_lifecycle(data: Mapping[str, Any]) -> bool:
    """Bind the no-sale native claim, donations, settlement, foreign CW20 and releases.

    The catalog's phase markers are descriptive only. This task's semantic
    checkpoint is derived from the one selected scenario and the receipts it
    records, so a fabricated phase-name list cannot close the coverage row.
    """
    scenarios = _obj(data.get("scenarios"))
    scenario = _obj(scenarios.get("no-sale"))
    terms = _obj(scenario.get("terms"))
    accounts = _obj(scenario.get("accounts"))
    contracts = _obj(scenario.get("contracts"))
    root_accounts = _obj(data.get("accounts"))
    root_contracts = _obj(data.get("contracts"))
    target = _as_int(terms.get("target_epoch"))
    host = accounts.get("host")
    deal = contracts.get("deal")
    foreign = contracts.get("foreign_cw20") or root_contracts.get("foreign_cw20")
    buyer = root_accounts.get("buyer")
    if (
        target is None or not _nonempty_str(host) or not _nonempty_str(deal)
        or not _nonempty_str(foreign) or not _nonempty_str(buyer)
        or accounts.get("buyer") is not None
    ):
        return False
    phases = scenario.get("phases")
    if not isinstance(phases, list):
        return False

    def exactly_one(name: str) -> Optional[Mapping[str, Any]]:
        matches = [p for p in phases if isinstance(p, Mapping) and p.get("name") == name]
        return matches[0] if len(matches) == 1 else None

    native = exactly_one("native_auto_claim")
    lock = exactly_one("lock")
    contamination = exactly_one("foreign_cw20_contamination")
    settlement = exactly_one("settle_claim")
    early_release = exactly_one("early_release_rejected")
    donations = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "liquid_donation"]
    addition = exactly_one("vesting_addition")
    releases = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "release"]
    if (
        native is None or lock is None or contamination is None or settlement is None
        or early_release is None
        or len(donations) != 2 or addition is None or len(releases) != 3
    ):
        return False

    native_index = phases.index(native)
    lock_index = phases.index(lock)
    contamination_index = phases.index(contamination)
    settlement_index = phases.index(settlement)
    early_release_index = phases.index(early_release)

    summary = _obj(_obj(native.get("summary")).get("epochPerformanceSummary"))
    recipient_rows = _obj(native.get("recipient_query")).get("entries")
    work = _as_int(summary.get("earned_coins", 0))
    reward = _as_int(summary.get("rewarded_coins", 0))
    if (
        native.get("level") != "live_network" or summary.get("claimed") is not True
        or _as_int(summary.get("epoch_index")) != target
        or summary.get("participant_id") != host
        or work is None or reward is None or work < 0 or reward < 0 or work + reward <= 0
        or not isinstance(recipient_rows, list)
        or len([row for row in recipient_rows if isinstance(row, Mapping)
                and _as_int(row.get("epoch")) == target and row.get("recipient") == deal]) != 1
    ):
        return False

    lock_tx = _obj(lock.get("tx"))
    before_lock = _obj(lock.get("state_before"))
    after_lock = _obj(lock.get("state_after"))
    bracket = _obj(lock.get("epoch_bracket"))
    lock_epoch = _as_int(bracket.get("epoch"))
    if (
        not _tx_included(lock_tx) or lock_epoch is None or not target <= lock_epoch <= target + 4
        or not _bracket_binds_tx(bracket, lock_epoch, lock_tx)
        or not _no_bank_transfer_involving(lock_tx, deal)
        or before_lock.get("status") not in ("open", "funded")
        or before_lock.get("buyer") is not None
        or before_lock.get("recipient_locked") is not False
        or after_lock.get("status") != "locked" or after_lock.get("buyer") is not None
        or after_lock.get("recipient_locked") is not True
    ):
        return False

    donations_by_label = {p.get("label"): p for p in donations}
    if set(donations_by_label) != {"before_settlement", "after_settlement"}:
        return False
    before_donation_index = phases.index(donations_by_label["before_settlement"])
    after_donation_index = phases.index(donations_by_label["after_settlement"])
    if not (
        native_index < lock_index < before_donation_index
        < contamination_index < settlement_index < early_release_index
        < after_donation_index
    ):
        return False
    donation_txs: Dict[str, Mapping[str, Any]] = {}
    for label, phase in donations_by_label.items():
        amount = _as_int(phase.get("amount_ngonka"))
        tx = _obj(phase.get("tx"))
        event = _event_attributes(tx, "transfer")
        before = _obj(phase.get("before"))
        after = _obj(phase.get("after"))
        if (
            amount is None or amount <= 0 or not _tx_included(tx)
            or event is None or event.get("sender") != buyer or event.get("recipient") != deal
            or _coin_amount(event.get("amount"), "ngonka") != amount
            or _as_int(after.get("deal_bank")) is None
            or _as_int(before.get("deal_bank")) is None
            or _as_int(after.get("deal_bank")) - _as_int(before.get("deal_bank")) < amount
            or before.get("state") != after.get("state")
            or phase.get("observed_deal_delta_ngonka") != _as_int(after.get("deal_bank")) - _as_int(before.get("deal_bank"))
        ):
            return False
        donation_txs[label] = tx

    contam_tx = _obj(contamination.get("tx"))
    contam_transfer = _cw20_transfer_attributes(contam_tx, foreign)
    contam_before = _obj(contamination.get("before"))
    contam_after = _obj(contamination.get("after"))
    contam_amount = _as_int(contamination.get("amount"))
    contam_before_deal = _as_int(contam_before.get("deal"))
    contam_after_deal = _as_int(contam_after.get("deal"))
    contam_before_buyer = _as_int(contam_before.get("buyer"))
    contam_after_buyer = _as_int(contam_after.get("buyer"))
    if (
        contam_amount is None or contam_amount <= 0 or not _tx_included(contam_tx)
        or contam_transfer is None or contam_transfer.get("from") != buyer
        or contam_transfer.get("to") != deal or _as_int(contam_transfer.get("amount")) != contam_amount
        or contam_before_deal is None or contam_after_deal is None
        or contam_before_buyer is None or contam_after_buyer is None
        or contam_after_deal - contam_before_deal != contam_amount
        or contam_before_buyer - contam_after_buyer != contam_amount
        or contam_before.get("state") != contam_after.get("state")
    ):
        return False

    # Settlement must implement the no-Buyer native oracle, with no CW20 payout
    # invented for a Buyer. Both native claim identity and the settled state
    # are independently checked against the scenario's terms.
    settle_tx = _obj(settlement.get("tx"))
    settle_summary = _obj(_obj(settlement.get("summary")).get("epochPerformanceSummary"))
    settle_before = _obj(settlement.get("before"))
    settle_after = _obj(settlement.get("after"))
    expected = _obj(settlement.get("expected"))
    actual = _obj(settlement.get("actual"))
    expected_cw20 = _obj(expected.get("cw20_deltas"))
    actual_cw20 = _obj(actual.get("cw20_deltas"))
    settle_event = _event_attributes(settle_tx, "wasm-claim_settled")
    if (
        not _tx_included(settle_tx) or settle_event is None
        or settle_event.get("_contract_address") != deal
        or settle_event.get("deal") != deal or settle_event.get("host") != host
        or settle_summary != summary
        or _obj(settle_before.get("state")).get("status") != "locked"
        or _obj(settle_before.get("state")).get("buyer") is not None
        or expected.get("status") != "releasing"
        or _as_int(expected.get("work_ngonka")) != work
        or _as_int(expected.get("reward_ngonka")) != reward
        or _as_int(expected.get("total_claim_ngonka")) != work + reward
        or _as_int(expected.get("buyer_entitlement_ngonka")) != 0
        or _as_int(expected.get("host_entitlement_ngonka")) != work + reward
        or _as_int(expected.get("gross_usdt")) != 0
        or _as_int(expected.get("fee_usdt")) != 0
        or _as_int(expected.get("host_net_usdt")) != 0
        or _as_int(expected.get("buyer_refund_usdt")) != 0
        or _as_int(expected.get("deal_outflow")) != 0
        or _as_int(expected_cw20.get("host")) != 0
        or _as_int(expected_cw20.get("fee_recipient")) != 0
        or _as_int(expected_cw20.get("buyer")) != 0
        or actual_cw20 != expected_cw20
        or _as_int(settle_before.get("foreign_cw20")) != contam_after_deal
        or _as_int(settle_after.get("foreign_cw20")) != contam_after_deal
        or _as_int(actual.get("deal_outflow")) != _as_int(expected.get("deal_outflow"))
        or _obj(settle_after.get("state")).get("status") != "releasing"
        or _obj(settle_after.get("state")).get("buyer") is not None
        or _as_int(_obj(settle_after.get("cw20")).get("deal")) != 0
        or not _validate_early_release_rejected(early_release, scenario, root_accounts)
    ):
        return False

    # The known vesting oracle validates the governance event and schedule.
    if not _validate_vesting_addition(addition):
        return False
    snapshots = [p for p in phases if isinstance(p, Mapping) and p.get("name") == "vesting_snapshot"]
    before_addition = [p for p in snapshots if p.get("label") == "before-additional-vesting"]
    addition_index = phases.index(addition)
    snapshot_index = phases.index(before_addition[0]) if len(before_addition) == 1 else -1
    if (
        len(before_addition) != 1 or before_addition[0].get("required_non_empty") is not True
        or snapshot_index >= addition_index or addition_index >= after_donation_index
    ):
        return False
    old_schedule = before_addition[0].get("normalized_epoch_amounts")
    if not isinstance(old_schedule, list) or not old_schedule:
        return False

    facts = [_validate_release_receipt(p) for p in releases]
    if any(item is None for item in facts):
        return False
    release_indices = [phases.index(p) for p in releases]
    initial, first, second = facts
    # Consume spendable donations before expecting a zero-balance rejection.
    # Bind that real payout to settlement and the subsequent early probe;
    # later vesting releases continue from its counters and frozen policy.
    if (
        not settlement_index < release_indices[0] < early_release_index
        or initial["deal"] != deal or initial["host"] != host
        or initial["buyer"] is not None
        or releases[0].get("state_before") != _obj(settle_after.get("state"))
        or releases[0].get("state_after") != _obj(_obj(early_release.get("before")).get("state"))
        or releases[0].get("state_after") != releases[1].get("state_before")
        or initial["bank_after"]["deal"] != 0
        or initial["height"] >= (_as_int(_obj(early_release.get("attempt")).get("height")) or 0)
        or initial["frozen_state"] != first["frozen_state"]
        or _as_int(releases[0].get("foreign_cw20_before")) != contam_after_deal
        or _as_int(releases[0].get("foreign_cw20_after")) != contam_after_deal
        or initial["bank_before"]["deal"] < _as_int(
            _obj(donations_by_label["before_settlement"].get("after")).get("deal_bank")
        )
    ):
        return False
    after_donation_deal_balance = _as_int(
        _obj(donations_by_label["after_settlement"].get("after")).get("deal_bank")
    )
    if (
        first["deal"] != deal or second["deal"] != deal
        or first["host"] != host or second["host"] != host
        or first["buyer"] is not None or second["buyer"] is not None
        or first["height"] >= second["height"]
        or not addition_index < release_indices[1] < release_indices[2]
        or first["frozen_state"] != second["frozen_state"]
        or releases[1].get("state_after") != releases[2].get("state_before")
        or _as_int(releases[1].get("foreign_cw20_before")) != _as_int(contam_after.get("deal"))
        or _as_int(releases[1].get("foreign_cw20_after")) != _as_int(contam_after.get("deal"))
        or _as_int(releases[2].get("foreign_cw20_after")) != _as_int(contam_after.get("deal"))
        or after_donation_deal_balance is None
        or first["bank_before"]["deal"] < after_donation_deal_balance
    ):
        return False

    # The donation after settlement must be reflected in the first release's
    # opening Deal balance, while the earlier one predates settlement.
    before_donation = donations_by_label["before_settlement"]
    after_donation = donations_by_label["after_settlement"]
    if (
        before_donation["tx"].get("height") is None or after_donation["tx"].get("height") is None
        or (_as_int(before_donation["tx"].get("height")) or 0) >= (_as_int(settle_tx.get("height")) or 0)
        or (_as_int(after_donation["tx"].get("height")) or 0) <= (_as_int(settle_tx.get("height")) or 0)
        or (_as_int(after_donation["tx"].get("height")) or 0)
        <= (_as_int(_obj(early_release.get("attempt")).get("height")) or 0)
        or (_as_int(after_donation["tx"].get("height")) or 0) >= first["height"]
    ):
        return False
    return True


def _validate_early_release_rejected(
    phase: Mapping[str, Any], scope: Mapping[str, Any],
    root_accounts: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Require an included pre-unlock NothingToRelease with no business-state change."""
    deal = _obj(scope.get("contracts")).get("deal")
    before = _obj(phase.get("before"))
    after = _obj(phase.get("after"))
    attempt = _obj(phase.get("attempt"))
    proof = _obj(phase.get("proof"))
    expected = _obj(phase.get("expected"))
    before_bank = _int_map(before.get("bank_ngonka"))
    after_bank = _int_map(after.get("bank_ngonka"))
    if (
        phase.get("level") != "live_network" or phase.get("deal") != deal
        or not _nonempty_str(deal) or not _nonempty_str(phase.get("caller"))
        or not _attempt_rejected_and_included(attempt)
        or attempt.get("codespace") != "wasm"
        or "no additional gnk is currently available for release" not in str(attempt.get("raw_log", "")).lower()
        or proof.get("contract_error") != "NothingToRelease"
        or proof.get("layer") != "deliver_tx" or proof.get("codespace") != "wasm"
        or proof.get("tx_hash") != attempt.get("tx_hash")
        or _as_int(proof.get("height")) != _as_int(attempt.get("height"))
        or expected.get("contract_error") != "NothingToRelease"
        or expected.get("deal_balance_ngonka") != 0 or expected.get("buyer") is not None
        or expected.get("state_unchanged") is not True
        or not before_bank or not after_bank or set(before_bank) != set(after_bank)
        or before_bank.get("deal") != 0 or after_bank.get("deal") != 0
        or before.get("state") != after.get("state")
        or before.get("cw20") != after.get("cw20")
        or before.get("foreign_cw20") != after.get("foreign_cw20")
        or _obj(before.get("state")).get("status") != "releasing"
        or _obj(before.get("state")).get("buyer") is not None
    ):
        return False
    accounts = _obj(scope.get("accounts"))
    root_accounts = _obj(root_accounts)
    caller = phase.get("caller")
    tracked_roles = {
        "host": accounts.get("host"),
        "buyer": accounts.get("buyer") or root_accounts.get("buyer"),
        "fee_recipient": accounts.get("fee_recipient"),
        "deal": deal,
    }
    fee_roles = {role for role, address in tracked_roles.items() if address == caller}
    if not fee_roles:
        return False
    deltas: Dict[str, int] = {}
    for role in before_bank:
        delta = after_bank[role] - before_bank[role]
        if role in fee_roles:
            if delta > 0:
                return False
            deltas[role] = delta
        elif delta != 0:
            return False
    return (
        _obj(phase.get("fee_payer_deltas_ngonka")) == deltas
        and caller in {accounts.get("host"), root_accounts.get("buyer")}
    )


def _validate_r7_1_bank_sequence(scope: Mapping[str, Any]) -> bool:
    """Require one plan, two selected Bank failures, then one release and repeat."""
    phases = scope.get("phases")
    if not isinstance(phases, list):
        return False
    names = (
        "native_bank_release_fault_plan", "native_bank_release_rollback",
        "release", "scenario_release_repeat", "native_bank_release_retry",
    )
    selected = {
        name: [(i, p) for i, p in enumerate(phases)
               if isinstance(p, dict) and p.get("name") == name]
        for name in names
    }
    if any(len(selected[name]) != (2 if name == "native_bank_release_rollback" else 1)
           for name in names):
        return False
    plan_index, plan_phase = selected["native_bank_release_fault_plan"][0]
    (first_index, first), (second_index, second) = selected["native_bank_release_rollback"]
    release_index, release = selected["release"][0]
    repeat_index, repeat = selected["scenario_release_repeat"][0]
    retry_index, retry = selected["native_bank_release_retry"][0]
    if not plan_index < first_index < second_index < release_index < repeat_index < retry_index:
        return False

    snapshot = _obj(plan_phase.get("snapshot"))
    state = _obj(snapshot.get("state"))
    bank = _int_map(snapshot.get("bank_ngonka"))
    cw20 = _int_map(snapshot.get("cw20"))
    roles = {"host", "buyer", "fee_recipient", "deal"}
    if not _snapshot_sections_present(snapshot) or not bank or not cw20:
        return False
    if set(bank) != roles or set(cw20) != roles or bank["deal"] <= 0:
        return False
    expected = _expected_release(state, bank["deal"])
    if expected is None:
        return False
    old_buyer = _as_int(state.get("buyer_released_ngonka"))
    old_host = _as_int(state.get("host_released_ngonka"))
    if old_buyer is None or old_host is None:
        return False
    buyer_amount = expected["buyer_released"] - old_buyer
    host_amount = expected["host_released"] - old_host
    if buyer_amount <= 0 or host_amount <= 0:
        return False
    deal = _obj(scope.get("contracts")).get("deal")
    accounts = _obj(scope.get("accounts"))
    buyer, host = accounts.get("buyer"), accounts.get("host")
    plan = _obj(plan_phase.get("plan"))
    if (
        not all(_nonempty_str(v) for v in (deal, buyer, host)) or buyer == host
        or plan.get("deal") != deal or plan.get("buyer") != buyer
        or plan.get("host") != host
        or _as_int(plan.get("buyer_amount")) != buyer_amount
        or _as_int(plan.get("host_amount")) != host_amount
        or _as_int(plan.get("failing_send_index")) != 2
        or plan.get("allowed_earlier_recipient") != buyer
        or plan.get("rejected_recipient") != host
    ):
        return False

    vesting = _obj(plan_phase.get("vesting_proof"))
    total = _obj(vesting.get("total"))
    coins = total.get("total_amount")
    schedule = _vesting_epoch_amounts(vesting.get("schedule"))
    if vesting.get("fully_unlocked") is not True or schedule is None:
        return False
    if coins is not None:
        if not isinstance(coins, list):
            return False
        for coin in coins:
            if not isinstance(coin, dict) or not _nonempty_str(coin.get("denom")):
                return False
            amount = _as_int(coin.get("amount"))
            if amount is None or amount < 0 or (coin["denom"] == DEFAULT_DENOM and amount):
                return False
    if any(epoch.get(DEFAULT_DENOM, 0) for epoch in schedule):
        return False

    for index, fault, recipient, earlier in (
        (1, first, buyer, None), (2, second, host, buyer),
    ):
        selected_fault = _obj(fault.get("expected"))
        restriction = _obj(fault.get("restriction"))
        attempt = _obj(fault.get("attempt"))
        if (
            fault.get("level") != "native_keeper_fault"
            or fault.get("before") != snapshot or fault.get("after") != snapshot
            or restriction.get("is_active") is not True
            or (_as_int(restriction.get("remaining_blocks")) or 0) <= 0
            or not _attempt_rejected_and_included(attempt, expected_codespace=None)
            or "user-to-user transfers are restricted"
            not in str(attempt.get("raw_log", "")).lower()
            or selected_fault.get("failure_category") != "bank_send_restriction"
            or _as_int(selected_fault.get("outgoing_transfer_index")) != index
            or selected_fault.get("rejected_recipient") != recipient
            or selected_fault.get("allowed_earlier_recipient") != earlier
            or selected_fault.get("state_and_tracked_balances_unchanged") is not True
        ):
            return False
        exemption = selected_fault.get("live_exemption")
        if index == 1:
            if exemption is not None:
                return False
        else:
            exemption = _obj(exemption)
            if (
                not _nonempty_str(exemption.get("exemption_id"))
                or exemption.get("from_address") != deal
                or exemption.get("to_address") != buyer
                or (_as_int(exemption.get("max_amount")) or 0) < buyer_amount
                or (_as_int(exemption.get("usage_limit")) or 0) <= 0
                or (_as_int(exemption.get("expiry_block")) or 0)
                <= (_as_int(restriction.get("current_block_height")) or 0)
            ):
                return False
    first_attempt = _obj(first.get("attempt"))
    second_attempt = _obj(second.get("attempt"))
    if (
        first_attempt.get("tx_hash") == second_attempt.get("tx_hash")
        or (_as_int(first_attempt.get("height")) or 0)
        >= (_as_int(second_attempt.get("height")) or 0)
    ):
        return False

    release_facts = _validate_release_receipt(release, strict=True)
    retry_expected = _obj(retry.get("expected"))
    if (
        release_facts is None or retry.get("level") != "native_keeper_fault"
        or retry.get("fault") != second or retry.get("successful_retry") != release
        or retry.get("repeat") != repeat
        or _obj(retry.get("restriction_after_fault")).get("is_active") is not False
        or _as_int(retry_expected.get("outgoing_transfer_index")) != 2
        or retry_expected.get("rejected_recipient") != host
        or retry_expected.get("fault_off_before_retry") is not True
        or retry_expected.get("exact_release_oracle") is not True
        or retry_expected.get("double_payout") is not False
        or release_facts["deal"] != deal or release_facts["buyer"] != buyer
        or release_facts["host"] != host
        or release_facts["buyer_delta"] != buyer_amount
        or release_facts["host_delta"] != host_amount
        or release.get("state_before") != state
        # Buyer and Host may receive network rewards while the restriction
        # expires. Their exact payout deltas are checked by the release receipt;
        # only the Deal's funds must stay equal to the rollback snapshot.
        or release_facts["bank_before"]["deal"] != bank["deal"]
    ):
        return False

    repeat_attempt = _obj(repeat.get("attempt"))
    rejection = _obj(repeat.get("terminal_rejection"))
    repeat_before = _obj(repeat.get("before"))
    repeat_expected = _obj(repeat.get("expected"))
    repeat_state = _obj(repeat_before.get("state"))
    if (
        repeat.get("level") != "native_keeper_fault" or repeat.get("deal") != deal
        or not _nonempty_str(repeat.get("caller"))
        or _as_int(repeat.get("caller_ngonka_delta")) is None
        or _as_int(repeat.get("caller_ngonka_delta")) > 0
        or not _attempt_rejected_and_included(repeat_attempt)
        or _TERMINAL_NOTHING_TO_RELEASE_MARKER
        not in str(repeat_attempt.get("raw_log", "")).lower()
        or rejection.get("contract_error") != "NothingToRelease"
        or rejection.get("layer") != "deliver_tx"
        or rejection.get("codespace") != "wasm"
        or _as_int(rejection.get("code")) != _as_int(repeat_attempt.get("code"))
        or str(rejection.get("matched_contract_error", "")).lower()
        != _TERMINAL_NOTHING_TO_RELEASE_MARKER
        or rejection.get("tx_hash") != repeat_attempt.get("tx_hash")
        or _as_int(rejection.get("height")) != _as_int(repeat_attempt.get("height"))
        or (_as_int(repeat_attempt.get("height")) or 0) <= release_facts["height"]
        or repeat.get("before") != repeat.get("after")
        or not _snapshot_sections_present(repeat_before)
        or any(set(_int_map(repeat_before.get(group)) or ()) != roles
               for group in ("cw20", "bank_ngonka"))
        or _as_int(_obj(repeat_before.get("bank_ngonka")).get("deal")) != 0
        or any(_as_int(_obj(repeat_before.get("bank_ngonka")).get(role))
               != release_facts["bank_after"][role]
               for role in ("deal", "buyer", "host"))
        or repeat_before.get("state") != release.get("state_after")
        or repeat_state.get("status") not in {"completed", "refunded"}
        or repeat_expected.get("selected_scenario_deal") != deal
        or repeat_expected.get("terminal_status") != repeat_state.get("status")
        or _as_int(repeat_expected.get("repeat_payout")) != 0
        or repeat_expected.get("arbitrary_error_accepted") is not False
    ):
        return False
    return True


def _validate_gift_release_chain(
    scope: Mapping[str, Any],
    unlocked: Mapping[str, Any],
    unlocked_observation: Tuple[int, int],
    final_bank: Mapping[str, int],
    final_state: Mapping[str, Any],
    final_observation: Tuple[int, int],
    baseline_counters: Mapping[str, int],
    final_counters: Mapping[str, int],
    gift: int,
) -> bool:
    """The gift must be paid out by real, unbroken release receipts.

    Only the receipts recorded between the first_unlocked and final
    observations count, so the two releases of the original claim settlement
    cannot be reused. Together they must start at the state the unlocked stage
    observed, chain state-by-state without gaps, drain exactly the gift and end
    on the final stage's counters and balances.
    """
    unlocked_epoch, unlocked_height = unlocked_observation
    final_epoch, final_height = final_observation

    window: List[Dict[str, Any]] = []
    for phase in _scope_phases(scope, "release"):
        bracket = _obj(phase.get("epoch_bracket"))
        height = _as_int(bracket.get("tx_height"))
        epoch = _as_int(bracket.get("epoch"))
        if height is None or epoch is None:
            continue
        # The snapshot is taken first, the payout follows, the final snapshot
        # is taken last; anything outside that interval is a different period.
        # The bounds are inclusive because a query and the next transaction can
        # legitimately land in the same block.
        if not unlocked_height <= height <= final_height:
            continue
        if not unlocked_epoch <= epoch <= final_epoch:
            continue
        receipt = _validate_release_receipt(phase, strict=True)
        if receipt is None:
            return False
        window.append(receipt)

    if not window:
        return False
    if len({receipt["tx_hash"] for receipt in window}) != len(window):
        return False
    window.sort(key=lambda receipt: receipt["height"])
    heights = [receipt["height"] for receipt in window]
    if any(heights[index] >= heights[index + 1] for index in range(len(heights) - 1)):
        return False

    # Every receipt must name this scenario's Deal and its two recipients.
    accounts = _obj(scope.get("accounts"))
    contracts = _obj(scope.get("contracts"))
    deal = contracts.get("deal")
    buyer = accounts.get("buyer")
    host = accounts.get("host")
    if not _nonempty_str(deal) or not _nonempty_str(buyer) or not _nonempty_str(host):
        return False
    for receipt in window:
        if (receipt["deal"], receipt["buyer"], receipt["host"]) != (deal, buyer, host):
            return False
        # release_scenario only accepts a release that conserves the whole
        # spendable balance, so a caller who is also a recipient must not have
        # paid a fee out of that same balance.
        if receipt["caller"] == buyer and receipt["caller_fee_delta"] != receipt["buyer_delta"]:
            return False
        if receipt["caller"] == host and receipt["caller_fee_delta"] != receipt["host_delta"]:
            return False

    unlocked_bank = unlocked["bank"]
    # The Deal fixes its entitlement and release policy at settlement. A
    # receipt can recompute correct payouts from a forged but equivalent
    # policy, so each receipt must also match both observed stage states.
    frozen_unlocked = {
        key: value for key, value in unlocked["state"].items()
        if key not in _RELEASE_MUTABLE_STATE_KEYS
    }
    frozen_final = {
        key: value for key, value in final_state.items()
        if key not in _RELEASE_MUTABLE_STATE_KEYS
    }
    if not frozen_unlocked or _normalized(frozen_unlocked) != _normalized(frozen_final):
        return False
    if any(
        _normalized(receipt["frozen_state"]) != _normalized(frozen_unlocked)
        for receipt in window
    ):
        return False
    first, last = window[0], window[-1]
    # The first payout must spend exactly the liquid amount the unlocked stage
    # observed, and the chain must start from the counters of that moment.
    if first["available"] != unlocked["liquid"]:
        return False
    if first["bank_before"]["deal"] != unlocked["liquid"]:
        return False
    if first["before"] != dict(baseline_counters):
        return False
    for previous, current in zip(window, window[1:]):
        if current["before"] != previous["after"]:
            return False
    if last["after"] != dict(final_counters):
        return False
    if last["bank_after"]["deal"] != 0 or final_bank["deal"] != 0:
        return False

    # Buyer and Host only gain in this window, so the receipts must sit inside
    # the two observed snapshots rather than describe some other balances.
    for role in ("buyer", "host"):
        if first["bank_before"][role] < unlocked_bank[role]:
            return False
        if final_bank[role] < last["bank_after"][role]:
            return False

    # Every coin of the gift left the Deal through these receipts, and their
    # payouts add up to exactly the movement the final counters claim.
    if sum(receipt["available"] for receipt in window) != gift:
        return False
    for counter, key in (
        ("buyer_delta", "buyer_released_ngonka"),
        ("host_delta", "host_released_ngonka"),
    ):
        moved = sum(receipt[counter] for receipt in window)
        if moved != final_counters[key] - baseline_counters[key]:
            return False
    return True


def _validate_r2_sequence(scope: Mapping[str, Any]) -> Set[str]:
    """Validates the whole R2.1 vested-gift story as a single proof.

    The governance addition, the four stage snapshots and the coins that
    actually moved are bound together here: one gift amount, one ordered
    sequence of observations and a final payout that the release oracle and the
    Buyer/Host bank deltas both confirm. Fragments that are individually
    well-formed but describe different gifts prove nothing.
    """
    checkpoints: Set[str] = set()
    gift_phases = _scope_phases(scope, "r2_gift_checkpoint")
    additions = [
        addition
        for addition in _scope_phases(scope, "vesting_addition")
        if _validate_vesting_addition(addition)
        and (
            "vesting_event_model" not in addition
            or _obj(scope.get("contracts")).get("deal") == addition.get("recipient")
        )
    ]

    if not gift_phases:
        # verify-vesting-addition-scenario also runs standalone (funded-claim's
        # no-sale scenario), where there is no gift sequence to bind it to.
        if additions:
            checkpoints.add("vesting_addition_verified")
        return checkpoints

    by_stage: Dict[str, Mapping[str, Any]] = {}
    for phase in gift_phases:
        stage = phase.get("stage")
        if stage not in _R2_STAGES or stage in by_stage:
            return set()
        by_stage[stage] = phase
    if set(by_stage) != set(_R2_STAGES):
        return set()

    snapshots: Dict[str, Dict[str, Any]] = {}
    observations: Dict[str, Tuple[int, int]] = {}
    gifts: Dict[str, int] = {}
    for stage, phase in by_stage.items():
        snapshot = phase.get("snapshot")
        if not _r2_snapshot_complete(snapshot):
            return set()
        snapshots[stage] = _obj(snapshot)
        epoch = _obj(phase.get("epoch"))
        observed_epoch = _as_int(epoch.get("epoch"))
        observed_height = _as_int(epoch.get("height"))
        if observed_epoch is None or observed_height is None:
            return set()
        observations[stage] = (observed_epoch, observed_height)
        amount = _as_int(phase.get("gift_amount_ngonka"))
        if amount is None or amount < 0:
            return set()
        gifts[stage] = amount

    gift = gifts["fully_locked"]
    if gift <= 0:
        return set()
    if gifts["first_unlocked"] != gift or gifts["final"] != gift:
        return set()
    # pre_gift is recorded before the gift exists, so the producer's default 0
    # is as valid there as the amount itself.
    if gifts["pre_gift"] not in (0, gift):
        return set()

    # Exactly one recomputed governance addition, and it must fund this gift.
    if len(additions) != 1:
        return set()
    addition = additions[0]
    if _as_int(addition.get("amount_ngonka")) != gift:
        return set()
    addition_epoch = _as_int(addition.get("epoch"))
    if addition_epoch is None:
        return set()
    if not observations["pre_gift"][0] <= addition_epoch <= observations["fully_locked"][0]:
        return set()

    ordered = [observations[stage] for stage in _R2_STAGES]
    epochs = [epoch for epoch, _ in ordered]
    heights = [height for _, height in ordered]
    # pre_gift and fully_locked may share an epoch (the proposal does not wait
    # for one), but the unlock stages are separated by real epoch turns.
    if epochs[0] > epochs[1] or not epochs[1] < epochs[2] < epochs[3]:
        return set()
    if any(heights[index] >= heights[index + 1] for index in range(len(heights) - 1)):
        return set()

    baseline = snapshots["pre_gift"]
    baseline_state = _obj(baseline.get("state"))
    baseline_totals = _r2_native_totals(baseline)
    if baseline_totals is None or any(baseline_totals[:3]):
        return set()
    if baseline_state.get("status") != "completed":
        return set()
    total_claim = _as_int(baseline_state.get("total_claim_ngonka"))
    if total_claim is None or total_claim <= 0:
        return set()
    if not isinstance(baseline_state.get("gnk_release_policy"), dict):
        return set()
    if _vesting_total_ngonka(baseline) != 0:
        return set()

    baseline_bank: Dict[str, int] = {}
    for role in _R2_SNAPSHOT_ROLES:
        amount = _bank_ngonka(baseline, role)
        if amount is None or amount < 0:
            return set()
        baseline_bank[role] = amount
    if baseline_bank["deal"] != 0:
        return set()
    baseline_released = {key: _as_int(baseline_state.get(key)) for key in _R2_MUTABLE_STATE_KEYS}
    if any(value is None or value < 0 for value in baseline_released.values()):
        return set()

    # The sequence itself is proven; the addition is bound to this gift.
    checkpoints.add("vesting_addition_verified")

    locked = _r2_stage_facts(snapshots["fully_locked"], baseline, baseline_bank)
    unlocked = _r2_stage_facts(snapshots["first_unlocked"], baseline, baseline_bank)
    final = _r2_stage_facts(snapshots["final"], baseline, baseline_bank)

    if (
        locked is not None
        and locked["pending"] == gift
        and locked["liquid"] == 0
        and locked["schedule_total"] == gift
        and locked["tranches"] >= 2
        and locked["released"] == baseline_released
        and locked["bank"]["deal"] == 0
        # The locked schedule must be the very schedule the addition produced.
        # Two unreadable schedules must not compare equal as None == None.
        and _vesting_epoch_amounts(addition.get("after")) is not None
        and _vesting_epoch_amounts(snapshots["fully_locked"].get("vesting_schedule"))
        == _vesting_epoch_amounts(addition.get("after"))
    ):
        checkpoints.add("r2_gift_fully_locked")

    if (
        unlocked is not None
        and 0 < unlocked["pending"] < gift
        and unlocked["liquid"] > 0
        # Nothing of the gift may be lost at the first unlock.
        and unlocked["liquid"] + unlocked["pending"] == gift
        and unlocked["released"] == baseline_released
    ):
        checkpoints.add("r2_gift_first_unlocked")

    if final is not None and unlocked is not None:
        expected = _expected_release(baseline_state, gift)
        actual = {
            "released_total": final["released"]["released_total_ngonka"],
            "buyer_released": final["released"]["buyer_released_ngonka"],
            "host_released": final["released"]["host_released_ngonka"],
        }
        buyer_paid = actual["buyer_released"] - baseline_released["buyer_released_ngonka"]
        host_paid = actual["host_released"] - baseline_released["host_released_ngonka"]
        counters_proven = (
            expected is not None
            and actual == {key: expected[key] for key in actual}
            and final["pending"] == 0
            and final["liquid"] == 0
            and final["schedule_total"] == 0
            and final["bank"]["deal"] == 0
            # The counters may only claim what the gift actually contained.
            and buyer_paid >= 0
            and host_paid >= 0
            and buyer_paid + host_paid == gift
            # Additional cross-check only: both recipients are live participants
            # that keep earning epoch rewards, so a multi-epoch balance trend can
            # bound the payout from below but never prove where the coins came
            # from. The receipt chain below does that.
            and _r2_payout_reached(unlocked["bank"], final["bank"], buyer_paid, host_paid)
            and _r2_payout_reached(baseline_bank, final["bank"], buyer_paid, host_paid)
            # Everything the Deal held at the first unlock was paid out.
            and unlocked["bank"]["deal"] - final["bank"]["deal"] == unlocked["liquid"]
        )
        # The origin of the coins is proven by the release transactions
        # themselves, with their own local Deal/Buyer/Host deltas.
        payout_proven = counters_proven and _validate_gift_release_chain(
            scope,
            unlocked,
            observations["first_unlocked"],
            final["bank"],
            final["state"],
            observations["final"],
            baseline_released,
            final["released"],
            gift,
        )
        if payout_proven:
            checkpoints.add("r2_gift_payout_receipts_verified")
            checkpoints.add("r2_gift_final_released")

    return checkpoints


def _native_unlock_amounts(value: Any) -> Optional[Dict[str, int]]:
    """Decode the native aggregate per denomination; duplicates are not totals."""
    if not isinstance(value, str) or not value:
        return None
    amounts: Dict[str, int] = {}
    for coin in value.split(","):
        match = re.fullmatch(r"([1-9][0-9]*)([a-zA-Z][a-zA-Z0-9/:._-]{2,127})", coin)
        if match is None or match[2] in amounts:
            return None
        try:
            amounts[match[2]] = int(match[1])
        except ValueError:
            return None
    return amounts


def _validate_ordered_vesting_addition(
    phase: Mapping[str, Any], before: List[Dict[str, int]],
    after: List[Dict[str, int]], addition: List[Dict[str, int]],
) -> bool:
    """Independently replay the native order, without trusting producer totals."""
    events = phase.get("vesting_events")
    recipient = phase.get("recipient")
    before_height = _as_int(phase.get("before_height"))
    after_height = _as_int(phase.get("after_height"))
    before_epoch = _as_int(phase.get("before_epoch"))
    after_epoch = _as_int(phase.get("after_epoch"))
    before_bank = _as_int(phase.get("before_bank_ngonka"))
    after_bank = _as_int(phase.get("after_bank_ngonka"))
    if (
        phase.get("vesting_event_model") != "ordered/2"
        or not isinstance(events, list) or not _nonempty_str(recipient)
        or any(value is None or value < 0 for value in (
            before_height, after_height, before_epoch, after_epoch, before_bank, after_bank
        ))
    ):
        return False
    if before_height <= 0 or after_height < before_height or after_epoch < before_epoch:
        return False
    if _as_int(phase.get("epoch")) != after_epoch:
        return False
    proposal_record = _obj(_obj(phase.get("proposal")).get("proposal"))
    messages = proposal_record.get("messages")
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], Mapping):
        return False
    message = messages[0]
    transfer = message.get("value")
    if message.get("type") != "inference/x/streamvesting/MsgTransferWithVesting" or not isinstance(transfer, Mapping):
        return False
    sender = transfer.get("sender")
    if (
        not _nonempty_str(sender)
        or transfer.get("recipient") != recipient
        or transfer.get("amount") != [{"denom": DEFAULT_DENOM, "amount": str(phase.get("amount_ngonka"))}]
        or _as_int(transfer.get("vesting_epochs")) != len(addition)
    ):
        return False
    for field in ("before", "after"):
        response = phase.get(field)
        if not isinstance(response, Mapping) or "vesting_schedule" not in response:
            return False
        native_schedule = response["vesting_schedule"]
        if native_schedule is not None and (
            not isinstance(native_schedule, Mapping)
            or native_schedule.get("participant_address") != recipient
        ):
            return False
    schedule = [dict(epoch) for epoch in before]
    previous = (before_height, -1)
    additions = released = released_count = 0
    unlocks = []
    unlock_heights = set()
    for event in events:
        if not isinstance(event, Mapping):
            return False
        height = _as_int(event.get("height"))
        index = _as_int(event.get("event_index"))
        attributes = event.get("attributes")
        if (
            height is None or index is None or index < 0
            or not before_height < height <= after_height
            or (height, index) <= previous or not isinstance(attributes, list)
        ):
            return False
        previous = (height, index)
        values = {}
        for item in attributes:
            if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
                return False
            if item["key"] in values:
                return False
            values[item["key"]] = item.get("value")
        if event.get("type") == "transfer_with_vesting":
            additions += 1
            amount = sum(epoch.get(DEFAULT_DENOM, 0) for epoch in addition)
            if (
                additions != 1 or values.get("recipient") != recipient
                or values.get("sender") != sender
                or values.get("amount") != f"{amount}{DEFAULT_DENOM}"
                or _as_int(values.get("vesting_epochs")) != len(addition)
            ):
                return False
            while len(schedule) < len(addition):
                schedule.append({})
            for i, epoch in enumerate(addition):
                for denom, amount in epoch.items():
                    schedule[i][denom] = schedule[i].get(denom, 0) + amount
                schedule[i] = {denom: amount for denom, amount in schedule[i].items() if amount}
        elif event.get("type") == "unlock_tokens":
            unlocked_amounts = _native_unlock_amounts(values.get("unlocked_amount"))
            unlocked = _as_int(values.get("participants_unlocked"))
            processed = _as_int(values.get("participants_processed"))
            if (
                height in unlock_heights or unlocked is None or processed is None
                or unlocked <= 0 or processed < unlocked
                or unlocked_amounts is None
            ):
                return False
            unlock_heights.add(height)
            unlocks.append(event)
            if schedule:
                if any(unlocked_amounts.get(denom, 0) < amount for denom, amount in schedule[0].items()):
                    return False
                released += schedule.pop(0).get(DEFAULT_DENOM, 0)
                released_count += 1
        else:
            return False
    return (
        additions == 1 and schedule == after
        and _normalized(phase.get("expected")) == _normalized(schedule)
        and after_bank - before_bank == released
        and _as_int(phase.get("released_prefix_count")) == released_count
        and _as_int(phase.get("released_prefix_ngonka")) == released
        and phase.get("unlock_events") == unlocks
        and _as_int(phase.get("eligible_prefix_count")) == len(unlocks)
    )


def _validate_vesting_addition(phase: Mapping[str, Any]) -> bool:
    """Recomputes the governance vesting addition instead of trusting it.

    divmod placement of the remainder, the untouched old tranches, the total
    conservation, the included funding receipt and a PASSED proposal are all
    mandatory.
    """
    amount = _as_int(phase.get("amount_ngonka"))
    epochs = _as_int(phase.get("vesting_epochs"))
    if amount is None or epochs is None or amount <= 0 or epochs <= 0:
        return False
    if phase.get("level") != "live_network":
        return False
    if _as_int(phase.get("epoch")) is None:
        return False

    before = _vesting_epoch_amounts(phase.get("before"))
    after = _vesting_epoch_amounts(phase.get("after"))
    if before is None or after is None:
        return False
    if not before and phase.get("allow_empty_before") is not True:
        return False

    quotient, remainder = divmod(amount, epochs)
    addition = [
        {DEFAULT_DENOM: quotient + (remainder if index == 0 else 0)} for index in range(epochs)
    ]
    if _normalized(phase.get("addition_by_epoch")) != _normalized(addition):
        return False

    funding_tx = _obj(phase.get("funding_tx"))
    if not _tx_included(funding_tx):
        return False
    proposal_record = _obj(_obj(phase.get("proposal")).get("proposal"))
    proposal_id = _as_int(phase.get("proposal_id"))
    if proposal_id is None or proposal_id <= 0 or _as_int(proposal_record.get("id")) != proposal_id:
        return False
    if proposal_record.get("status") != "PROPOSAL_STATUS_PASSED":
        return False
    proposal_messages = proposal_record.get("messages")
    if not isinstance(proposal_messages, list) or len(proposal_messages) != 1:
        return False
    proposal_message = proposal_messages[0]
    if not isinstance(proposal_message, Mapping):
        return False
    proposal_transfer = _obj(proposal_message.get("value"))
    proposal_sender = proposal_transfer.get("sender")
    recipients = [
        phase.get("recipient"),
        _obj(_obj(phase.get("before")).get("vesting_schedule")).get("participant_address"),
        _obj(_obj(phase.get("after")).get("vesting_schedule")).get("participant_address"),
    ]
    recipients = [recipient for recipient in recipients if recipient is not None]
    if (
        not recipients
        or any(not _nonempty_str(recipient) or recipient != recipients[0] for recipient in recipients)
        or proposal_message.get("type") != "inference/x/streamvesting/MsgTransferWithVesting"
        or proposal_transfer.get("recipient") != recipients[0]
        or proposal_transfer.get("amount") != [{"denom": DEFAULT_DENOM, "amount": str(amount)}]
        or _as_int(proposal_transfer.get("vesting_epochs")) != epochs
    ):
        return False
    funding_transfer = _event_attributes(funding_tx, "transfer")
    if (
        not _nonempty_str(proposal_sender)
        or funding_transfer is None
        or not _nonempty_str(funding_transfer.get("sender"))
        or funding_transfer.get("recipient") != proposal_sender
        or funding_transfer.get("amount") != f"{amount}{DEFAULT_DENOM}"
    ):
        return False
    if "vesting_event_model" in phase or "vesting_events" in phase:
        return _validate_ordered_vesting_addition(phase, before, after, addition)

    expected: List[Dict[str, int]] = []
    for index in range(max(len(before), len(addition))):
        left = before[index] if index < len(before) else {}
        right = addition[index] if index < len(addition) else {}
        combined = {}
        for denom in set(left) | set(right):
            total = left.get(denom, 0) + right.get(denom, 0)
            if total:
                combined[denom] = total
        expected.append(combined)
    if _normalized(phase.get("expected")) != _normalized(expected):
        return False
    release_fields = (
        "released_prefix_count",
        "released_prefix_ngonka",
        "before_bank_ngonka",
        "after_bank_ngonka",
        "before_epoch",
        "after_epoch",
        "eligible_prefix_count",
    )
    if all(field not in phase for field in release_fields):
        # Historical evidence was only valid when no prefix was released.
        released_count = 0
        released = 0
        if after != expected:
            return False
    else:
        released_count = _as_int(phase.get("released_prefix_count"))
        released = _as_int(phase.get("released_prefix_ngonka"))
        before_bank = _as_int(phase.get("before_bank_ngonka"))
        after_bank = _as_int(phase.get("after_bank_ngonka"))
        before_epoch = _as_int(phase.get("before_epoch"))
        after_epoch = _as_int(phase.get("after_epoch"))
        eligible_count = _as_int(phase.get("eligible_prefix_count"))
        if (
            released_count is None
            or released is None
            or before_bank is None
            or after_bank is None
            or before_epoch is None
            or after_epoch is None
            or eligible_count is None
            or released_count < 0
            or released_count > len(expected)
            or after_epoch < before_epoch
            or released_count > eligible_count
        ):
            return False
        # Native vesting is processed at a stage inside an epoch, so the
        # observations may legitimately span a boundary. Epoch-number
        # subtraction is not proof of how many unlocks occurred. Recompute
        # eligibility from recorded consensus events instead.
        before_height = _as_int(phase.get("before_height"))
        after_height = _as_int(phase.get("after_height"))
        unlock_events = phase.get("unlock_events")
        if (
            before_height is None or after_height is None
            or before_height <= 0 or after_height < before_height
            or not isinstance(unlock_events, list)
            or eligible_count != len(unlock_events)
        ):
            return False
        previous_height = before_height
        for event in unlock_events:
            if not isinstance(event, Mapping):
                return False
            height = _as_int(event.get("height"))
            if height is None or not previous_height < height <= after_height:
                return False
            # One summary per processed block; duplicates cannot authorize
            # additional tranches. Check the native summary's mandatory fields.
            attributes = event.get("attributes")
            if not isinstance(attributes, list):
                return False
            values = {}
            for attribute in attributes:
                if not isinstance(attribute, Mapping):
                    return False
                key = attribute.get("key")
                if not isinstance(key, str) or key in values:
                    return False
                values[key] = attribute.get("value")
            unlocked = _as_int(values.get("participants_unlocked"))
            processed = _as_int(values.get("participants_processed"))
            if (
                unlocked is None or processed is None
                or unlocked <= 0 or processed < unlocked
                or not _nonempty_str(values.get("unlocked_amount"))
            ):
                return False
            previous_height = height
        if after != expected[released_count:]:
            return False
        recomputed_release = sum(
            epoch.get(DEFAULT_DENOM, 0) for epoch in expected[:released_count]
        )
        if released != recomputed_release or after_bank - before_bank != released:
            return False

    after_total = sum(epoch.get(DEFAULT_DENOM, 0) for epoch in after) + released
    before_total = sum(epoch.get(DEFAULT_DENOM, 0) for epoch in before)
    if after_total - before_total != amount:
        return False

    return True


def validate_evidence_scopes(
    data: Mapping[str, Any],
    evidence_scopes: Sequence[str] = (),
) -> Optional[str]:
    """Every scenario scope a task declares must really exist in the context.

    Without this check a renamed or deleted scenario silently falls back to the
    remaining scenarios, letting a neighbouring fixture answer for the one under
    test.
    """
    if not evidence_scopes:
        return None
    scenarios = data.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, dict) else {}
    for raw_scope in evidence_scopes:
        scope = str(raw_scope)
        if scope == TOP_LEVEL_SCOPE:
            continue
        record = scenarios.get(scope)
        if not isinstance(record, dict) or not record:
            return (
                f"declared scenario scope {scope!r} is missing from "
                "live-context.json scenarios"
            )
        phases = record.get("phases")
        if not isinstance(phases, list) or not phases:
            return f"declared scenario scope {scope!r} has no recorded phases"
    return None


def _iter_scoped_phases(
    data: Mapping[str, Any],
    scenario_selector: Optional[str] = None,
    evidence_scopes: Sequence[str] = (),
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]], Set[str]]:
    """Yields (phase, owning_scope_record, owning_scope_name) without flattening.

    The owning record carries terms, accounts, contracts and the sibling phases,
    so cross-phase evidence can never be borrowed from a different scenario.
    When a task declares evidence_scopes, only those producer scopes are read;
    otherwise a selector that names no existing scenario yields no scenario
    phases at all instead of falling back to every scenario.
    """
    scenario_names: Set[str] = set()
    entries: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]] = []

    scenarios = data.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, dict) else {}
    declared = [str(scope) for scope in (evidence_scopes or [])]

    if declared:
        allow_top_level = TOP_LEVEL_SCOPE in declared
        allowed = [scope for scope in declared if scope != TOP_LEVEL_SCOPE]
    else:
        allow_top_level = True
        if scenario_selector and scenarios:
            allowed = [scenario_selector] if scenario_selector in scenarios else []
        else:
            allowed = list(scenarios)

    if allow_top_level:
        top_scope = {key: value for key, value in data.items() if key != "scenarios"}
        for phase in data.get("phases", []) or []:
            if isinstance(phase, dict):
                entries.append((phase, top_scope, None))

    for name in allowed:
        record = scenarios.get(name)
        if not isinstance(record, dict):
            continue
        scenario_names.add(str(name))
        for phase in record.get("phases", []) or []:
            if isinstance(phase, dict):
                entries.append((phase, record, str(name)))

    return entries, scenario_names


def extract_and_validate_scenario_predicates(
    data: Mapping[str, Any],
    scenario_selector: Optional[str] = None,
    evidence_scopes: Sequence[str] = (),
) -> Set[str]:
    """Inspects phases and scenario records in live-context.json,
    evaluating real predicates and assertions to derive verified semantic checkpoints.
    Does NOT blindly accept top-level keys or unverified checkpoint strings.
    """
    observed: Set[str] = set()
    r2_scopes_seen: Set[int] = set()
    routing_verified_scopes: Set[str] = set()

    if "bootstrap" in data:
        observed.add("bootstrap")

    entries, scenario_names = _iter_scoped_phases(data, scenario_selector, evidence_scopes)
    observed |= scenario_names

    for p, scope, _scope_name in entries:
        terms = _obj(scope.get("terms"))
        p_name = p.get("name")
        if not p_name or not isinstance(p_name, str):
            continue
        observed.add(f"phase:{p_name}")
        observed.add(p_name)

        if p_name == "factory_isolation" and _scope_name is None:
            if _validate_factory_isolation(p, data):
                observed.add("factory_isolation_verified")

        if p_name == "lock_exact_e":
            if _validate_successful_lock_phase(p, terms, offset=0):
                observed.add("state_transition:Funded->Locked")
                observed.add("recipient_locked=true")
                observed.add("no_deal_bank_transfer")
                observed.add("lock_tx_included_in_epoch_e")
                observed.add("target_epoch_e_reached")

        elif p_name == "lock_e_plus_4":
            if _validate_successful_lock_phase(p, terms, offset=4):
                observed.add("state_transition:Funded->Locked")
                observed.add("recipient_locked=true")
                observed.add("no_deal_bank_transfer")
                observed.add("lock_tx_included_in_epoch_e_plus_4")
                observed.add("target_epoch_e_plus_4_reached")

        elif p_name == "lock_e_plus_5_rejected":
            if _validate_lock_rejected_phase(p, terms):
                observed.add("lock_window_closed")
                observed.add("deal_remains_funded")
                observed.add("target_epoch_e_plus_5_reached")

        elif p_name == "lock":
            before = _obj(p.get("before")) or _obj(p.get("state_before"))
            after = _obj(p.get("after")) or _obj(p.get("state_after"))
            b_state = _obj(before.get("state")) or before
            a_state = _obj(after.get("state")) or after
            if b_state.get("status") in ("funded", "open") and a_state.get("status") == "locked":
                observed.add("state_transition:Funded->Locked")
            if a_state.get("recipient_locked") is True:
                observed.add("recipient_locked=true")

        elif p_name == "terminal_release_repeat":
            if _validate_terminal_release_repeat(p):
                observed.add("terminal_rejection_nothing_to_release")
                observed.add("state_entitlements_unchanged")
                observed.add("no_deal_bank_transfer")

        elif p_name == "b3_foreign_native_successful_release":
            observed |= _validate_b3_foreign_native_phase(p)

        elif p_name == "late_completed_donation_release":
            if _validate_late_completed_donation_release(p):
                observed.add("two_liquid_donations_verified")
                observed.add("cumulative_rounding_asserted")

        elif p_name == "native_unclaimed_precondition":
            observed |= _validate_native_unclaimed_precondition(p, scope)

        elif p_name == "native_summary_absent":
            # Only the E+3 boundary observation proves the emergency deadline.
            if (
                _as_int(p.get("expected_offset")) == NETWORK_UNCONFIRMED_DELAY_EPOCHS
                and _validate_native_summary_absent(p, scope)
            ):
                observed.add("emergency_deadline_reached")

        elif p_name == "refund_rejected":
            observed |= _validate_refund_rejected(p, scope)

        elif p_name == "refund_committed":
            refund_checkpoints = _validate_refund_committed(p, scope)
            observed |= refund_checkpoints
            expected_reason = {
                "routing-mismatch": "routing_mismatch",
                "routing-missing": "routing_missing",
            }.get(_scope_name)
            if (
                expected_reason is not None
                and p.get("reason") == expected_reason
                and {"routing_refund_committed", "routing_refund_fault_rollback"}
                <= refund_checkpoints
            ):
                routing_verified_scopes.add(_scope_name)

        elif p_name == "release":
            # A release is only "completed" once its own receipt reproduces:
            # the transaction, its epoch bracket and the local bank deltas.
            if _validate_release_receipt(p) is not None:
                observed.add("release_completed")

        elif p_name == "claim_settle":
            settle_tx = _obj(p.get("settle_tx"))
            cw20_faults = p.get("cw20_fault_rollbacks")
            # A successful code alone says nothing about whether this was the
            # one correctly accounted settlement. Bind the checkpoint to the
            # transaction event, payout accounting and the scenario's required
            # recipient-targeted rollback proof.
            required_positions = (2,) if scenario_selector == "funded-claim" else (1, 2, 3)
            settlement_verified = (
                _tx_included(settle_tx)
                and _validate_cw20_fault_rollbacks(
                    cw20_faults, p, scope, required_positions
                )
            )
            if settlement_verified:
                observed.add("single_settlement_succeeds")
                if len(required_positions) == 3:
                    observed.add("cw20_three_send_rejections_asserted")
                    observed.add("settlement_atomic_rollback_verified")

        elif p_name == "early_release_rejected":
            if _validate_early_release_rejected(p, scope, _obj(data.get("accounts"))):
                observed.add("early_release_unavailable_verified")

        elif p_name == "native_bank_release_fault_plan":
            pass

        elif p_name == "native_bank_release_rollback":
            attempt = _obj(p.get("attempt"))
            raw_log = str(attempt.get("raw_log", "")).lower()
            restriction = _obj(p.get("restriction"))
            included_rejection = (
                p.get("level") == "native_keeper_fault"
                and restriction.get("is_active") is True
                and (_as_int(restriction.get("remaining_blocks")) or 0) > 0
                and _attempt_rejected_and_included(attempt, expected_codespace=None)
                and "user-to-user transfers are restricted" in raw_log
            )
            if included_rejection:
                observed.add("bank_send_rejection_asserted")
            before = p.get("before")
            if (
                included_rejection
                and _snapshot_sections_present(before)
                and before == p.get("after")
                and all(set(_int_map(_obj(before).get(group)) or ())
                        == {"host", "buyer", "fee_recipient", "deal"}
                        for group in ("cw20", "bank_ngonka"))
            ):
                observed.add("bank_rollback_atomic")

        elif p_name == "native_bank_release_retry":
            release = _obj(p.get("successful_retry"))
            restr = _obj(p.get("restriction_after_fault"))
            expected = _obj(p.get("expected"))
            fault = _obj(p.get("fault"))
            matching_faults = [
                prior for prior in _scope_phases(scope, "native_bank_release_rollback")
                if prior == fault
            ]
            release_facts = _validate_release_receipt(release, strict=True)
            if (
                release_facts is not None
                and len(matching_faults) == 1
                and _attempt_rejected_and_included(
                    _obj(fault.get("attempt")), expected_codespace=None
                )
                and fault.get("level") == "native_keeper_fault"
                and _obj(fault.get("restriction")).get("is_active") is True
                and "user-to-user transfers are restricted"
                in str(_obj(fault.get("attempt")).get("raw_log", "")).lower()
                and _snapshot_sections_present(fault.get("before"))
                and fault.get("before") == fault.get("after")
                and release_facts["deal"] == _obj(scope.get("contracts")).get("deal")
                and release_facts["bank_before"]["deal"]
                == _as_int(_obj(_obj(fault.get("before")).get("bank_ngonka")).get("deal"))
                and restr.get("is_active") is False
                and expected.get("double_payout") is False
            ):
                observed.add("bank_send_retry_succeeds")

        elif p_name == "r1_1_refund_e_plus_5_rejected":
            if _validate_r1_refund_rejected_phase(p, terms):
                observed.add("r1_refund_boundary_asserted")

        elif p_name in ("vesting_addition", "r2_gift_checkpoint"):
            # R2.1 is one proof, not a bag of phases: the gift, its stages and
            # the coins that moved are validated together, once per scope.
            scope_key = id(scope)
            if scope_key not in r2_scopes_seen:
                r2_scopes_seen.add(scope_key)
                observed |= _validate_r2_sequence(scope)

    if {"routing-mismatch", "routing-missing"} <= routing_verified_scopes:
        observed.add("both_routing_refunds_verified")
    if (
        # An older package's suite plan names the pre-rename selector; translate
        # it through the catalog's alias table rather than listing spellings here.
        (scenario_selector or "") == "native-release-rollback-retry"
        and TOP_LEVEL_SCOPE in evidence_scopes
        and _validate_r7_1_bank_sequence(data)
    ):
        observed.add("r7_1_two_bank_sends_verified")
    if (
        scenario_selector == "funded-claim"
        and TOP_LEVEL_SCOPE in evidence_scopes
        and _validate_funded_claim_path(data)
    ):
        observed.add("funded_claim_path_verified")
    if (
        scenario_selector == "funded-claim"
        and TOP_LEVEL_SCOPE in evidence_scopes
        and _validate_funded_release_lifecycle(data)
    ):
        observed.add("funded_release_lifecycle_verified")
    if (
        scenario_selector == "unfunded-lock-boundaries"
        and {"lock-e-plus-4", "lock-e-plus-5"} <= scenario_names
        and _validate_unfunded_lock_boundaries(data)
    ):
        observed.add("unfunded_lock_boundaries_verified")
    if (
        scenario_selector == "funded-gas-sweep"
        and "gas-claimed" in scenario_names
        and _validate_claimed_refund_gas_sweep(data)
    ):
        observed.add("claimed_refund_gas_sweep_verified")
    if (
        scenario_selector == "no-sale-vesting-lifecycle"
        and "no-sale" in scenario_names
        and _validate_no_sale_vesting_lifecycle(data)
    ):
        observed.add("no_sale_vesting_lifecycle_verified")
    return observed


#: Name and schemas of the harness' per-task immutability evidence
#: (``scripts/acceptance_harness.py run-live``), written into the task's evidence
#: directory beside ``live-context.json``.
SOURCE_IMMUTABILITY_FILENAME = "source-immutability.json"
SOURCE_IMMUTABILITY_SET_SCHEMA = "a8.source-immutability-set/1"
SOURCE_IMMUTABILITY_NETWORK_SCHEMA = "a8.source-immutability-set/2"
SOURCE_IMMUTABILITY_RECORD_SCHEMA = "a8.source-immutability/1"
#: Root labels the harness writes into ``source-immutability.json``.
SOURCE_IMMUTABILITY_ROOT_LABELS: Tuple[str, ...] = ("gonka", "marketplace")

#: ``live-context.json`` ``source`` fields of the retired overlay /
#: prepared-build models. Their mere presence in immutable-source evidence is
#: a contradiction: that evidence claims no tree other than the selected commit
#: ever existed, so a field naming one means the producer is not the one the
#: model declares.
RETIRED_PREPARED_SOURCE_FIELDS: Tuple[str, ...] = (
    "gonka_prepared_sha",
    "gonka_overlay_manifest_sha256",
    "gonka_test_harness_sha",
    "gonka_base_sha",
)

#: Evidence files whose SHA-256 the immutable-source live context declares,
#: keyed by the declaring ``source`` field. Each file is re-hashed from the
#: evidence directory; a declared hash of a file that is not there proves
#: nothing.
IMMUTABLE_SOURCE_HASHED_FILES: Tuple[Tuple[str, str], ...] = (
    ("source_immutability_sha256", SOURCE_IMMUTABILITY_FILENAME),
    ("external_harness_inputs_sha256", "external-harness/harness-inputs.json"),
    ("network_manifest_sha256", "network/network-manifest.json"),
)

_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _is_full_git_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_FULL_GIT_SHA.match(value))


def check_source_immutability_document(
    document: Optional[bytes],
    payload: Optional[Mapping[str, Any]],
    *,
    gonka_sha: Optional[str],
    marketplace_sha: Optional[str],
    network_manifest: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Grade one task's ``source-immutability.json`` against the plan and its live context.

    ``document`` is the raw file (``None`` when absent) and ``payload`` the
    parsed ``live-context.json`` of the same task. Returns ``{"status":
    MATCHED|INCOMPLETE|MISMATCHED, "document_sha256", "missing",
    "mismatches"}``.

    This is the ONE implementation of the rule. The suite verifier calls it
    here, and ``forward_e2e.execution.deployment.verify_source_immutability_evidence``
    delegates to it (the E2E layer may import the suite runner, never the
    reverse), so ``run``, ``report`` and the suite verifier cannot disagree
    about one file.

    Absence is never agreement: a missing or unreadable document, a missing
    root, SHA or verdict is ``INCOMPLETE``; anything that says a snapshot
    changed, names another commit, or carries a field of the retired
    prepared-build model is ``MISMATCHED``.
    """
    problems: List[Dict[str, Any]] = []
    missing: List[str] = []
    source = (payload or {}).get("source") if isinstance(payload, Mapping) else None
    source = source if isinstance(source, Mapping) else {}

    reported_gonka = source.get("gonka_sha")
    if not isinstance(reported_gonka, str) or not reported_gonka:
        missing.append("live-context.source.gonka_sha")
    elif gonka_sha and reported_gonka.lower() != str(gonka_sha).lower():
        problems.append({"field": "live-context.source.gonka_sha",
                         "expected": gonka_sha, "actual": reported_gonka})
    if marketplace_sha:
        reported_market = source.get("marketplace_commit_sha")
        if not isinstance(reported_market, str) or not reported_market:
            missing.append("live-context.source.marketplace_commit_sha")
        elif reported_market.lower() != str(marketplace_sha).lower():
            problems.append({"field": "live-context.source.marketplace_commit_sha",
                             "expected": marketplace_sha, "actual": reported_market})
    for retired in RETIRED_PREPARED_SOURCE_FIELDS:
        if source.get(retired) not in (None, ""):
            problems.append({"field": f"live-context.source.{retired}",
                             "reason": "a field of the retired prepared-build model is present"})

    declared_verdict = source.get("source_immutability_verdict")
    if declared_verdict is None:
        missing.append("live-context.source.source_immutability_verdict")
    elif declared_verdict != "UNCHANGED":
        problems.append({"field": "live-context.source.source_immutability_verdict",
                         "expected": "UNCHANGED", "actual": declared_verdict})

    document_sha: Optional[str] = None
    parsed: Optional[Mapping[str, Any]] = None
    if document is None:
        missing.append(SOURCE_IMMUTABILITY_FILENAME)
    else:
        document_sha = hashlib.sha256(document).hexdigest()
        declared_sha = source.get("source_immutability_sha256")
        if not isinstance(declared_sha, str) or not declared_sha:
            missing.append("live-context.source.source_immutability_sha256")
        elif declared_sha.lower() != document_sha:
            problems.append({"field": "live-context.source.source_immutability_sha256",
                             "expected": document_sha, "actual": declared_sha})
        try:
            candidate = json.loads(document.decode("utf-8"))
            parsed = candidate if isinstance(candidate, Mapping) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if parsed is None:
            missing.append(f"{SOURCE_IMMUTABILITY_FILENAME} (unreadable)")

    parsed_network: Optional[Mapping[str, Any]] = None
    if network_manifest is not None:
        try:
            candidate_network = json.loads(network_manifest.decode("utf-8"))
            parsed_network = candidate_network if isinstance(candidate_network, Mapping) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_network = None
        if parsed_network is None:
            missing.append("network/network-manifest.json (unreadable)")
        declared_network_sha = source.get("network_manifest_sha256")
        observed_network_sha = hashlib.sha256(network_manifest).hexdigest()
        if not isinstance(declared_network_sha, str) or not declared_network_sha:
            missing.append("live-context.source.network_manifest_sha256")
        elif declared_network_sha.lower() != observed_network_sha:
            problems.append({"field": "live-context.source.network_manifest_sha256",
                             "expected": observed_network_sha, "actual": declared_network_sha})

    if parsed is not None:
        if parsed.get("schema") not in (SOURCE_IMMUTABILITY_SET_SCHEMA, SOURCE_IMMUTABILITY_NETWORK_SCHEMA):
            problems.append({"field": "schema", "expected": SOURCE_IMMUTABILITY_NETWORK_SCHEMA,
                             "actual": parsed.get("schema")})
        roots = parsed.get("roots") if isinstance(parsed.get("roots"), Mapping) else {}
        expected_by_root = {"gonka": gonka_sha, "marketplace": marketplace_sha}
        for label in SOURCE_IMMUTABILITY_ROOT_LABELS:
            record = roots.get(label)
            if not isinstance(record, Mapping):
                missing.append(f"roots.{label}")
                continue
            if record.get("schema") != SOURCE_IMMUTABILITY_RECORD_SCHEMA:
                problems.append({"field": f"roots.{label}.schema",
                                 "expected": SOURCE_IMMUTABILITY_RECORD_SCHEMA,
                                 "actual": record.get("schema")})
            if record.get("label") != label:
                problems.append({"field": f"roots.{label}.label",
                                 "expected": label, "actual": record.get("label")})
            verdict = record.get("verdict")
            if verdict is None or verdict == "INCOMPLETE":
                missing.append(f"roots.{label}.verdict")
            elif verdict != "UNCHANGED":
                problems.append({"field": f"roots.{label}.verdict", "expected": "UNCHANGED",
                                 "actual": verdict,
                                 "violations": list(record.get("violations") or [])[:20],
                                 "differences": list(record.get("differences") or [])[:20]})
            expected = expected_by_root.get(label)
            actual = record.get("expected_sha")
            if expected and (not isinstance(actual, str) or actual.lower() != str(expected).lower()):
                problems.append({"field": f"roots.{label}.expected_sha",
                                 "expected": expected, "actual": actual})
            before, after = record.get("before"), record.get("after")
            for phase, fingerprint in (("before", before), ("after", after)):
                if not isinstance(fingerprint, Mapping):
                    missing.append(f"roots.{label}.{phase}")
            if isinstance(before, Mapping) and isinstance(after, Mapping) and expected:
                try:
                    recomputed = source_snapshot.immutability_record(
                        label=label, expected_sha=expected, before=before, after=after
                    )
                except (source_snapshot.SourceSnapshotError, TypeError, ValueError, KeyError) as exc:
                    problems.append({"field": f"roots.{label}.fingerprints",
                                     "reason": f"cannot validate saved snapshots: {exc}"})
                else:
                    for field in ("verdict", "violations", "differences"):
                        if record.get(field) != recomputed[field]:
                            problems.append({"field": f"roots.{label}.{field}",
                                             "expected": recomputed[field], "actual": record.get(field)})
                    if recomputed["verdict"] != "UNCHANGED":
                        problems.append({"field": f"roots.{label}.fingerprints",
                                         "reason": "saved snapshots do not prove unchanged sources",
                                         "violations": recomputed["violations"][:20],
                                         "differences": recomputed["differences"][:20]})
        network_record = parsed.get("network_root")
        full_working_tree = (
            parsed_network is not None
            and parsed_network.get("copy_mode") == "full_working_tree"
        )
        if parsed.get("schema") == SOURCE_IMMUTABILITY_NETWORK_SCHEMA and parsed_network is not None and not full_working_tree:
            problems.append({"field": "network/network-manifest.json.copy_mode",
                             "expected": "full_working_tree",
                             "actual": parsed_network.get("copy_mode")})
        if (full_working_tree or parsed.get("schema") == SOURCE_IMMUTABILITY_NETWORK_SCHEMA) and not isinstance(network_record, Mapping):
            missing.append("network_root")
        if parsed.get("schema") == SOURCE_IMMUTABILITY_NETWORK_SCHEMA and parsed_network is None:
            missing.append("network/network-manifest.json")
        if isinstance(network_record, Mapping):
            network_verdict = network_record.get("verdict")
            if network_verdict in (None, "INCOMPLETE"):
                missing.append("network_root.verdict")
            elif network_verdict != "UNCHANGED":
                problems.append({"field": "network_root.verdict", "expected": "UNCHANGED",
                                 "actual": network_verdict})
            violations = network_record.get("violations")
            if not isinstance(violations, list):
                missing.append("network_root.violations")
            elif violations:
                problems.append({"field": "network_root.violations", "expected": [],
                                 "actual": violations[:20]})
            if not isinstance(network_record.get("runtime_additions"), list):
                missing.append("network_root.runtime_additions")
            if parsed_network is not None:
                recorded_post_run = parsed_network.get("post_run_verification")
                if not isinstance(recorded_post_run, Mapping):
                    missing.append("network/network-manifest.json.post_run_verification")
                elif dict(recorded_post_run) != dict(network_record):
                    problems.append({"field": "network_root", "reason":
                                     "source record and network manifest disagree"})
                for count_field, entries_field in (
                    ("checked_upstream_files", "upstream_copies"),
                    ("checked_generated_files", "generated"),
                ):
                    entries = parsed_network.get(entries_field)
                    if not isinstance(entries, list):
                        missing.append(f"network/network-manifest.json.{entries_field}")
                    elif network_record.get(count_field) != len(entries):
                        problems.append({"field": f"network_root.{count_field}",
                                         "expected": len(entries), "actual": network_record.get(count_field)})
                for item in parsed_network.get("upstream_copies") or []:
                    if not isinstance(item, Mapping) or item.get("sha256") != item.get("source_sha256"):
                        problems.append({"field": "network/network-manifest.json.upstream_copies",
                                         "reason": "a copied file differs from its selected source"})
                        break
        verdict = parsed.get("verdict")
        if verdict is None or verdict == "INCOMPLETE":
            missing.append("verdict")
        elif verdict != "UNCHANGED":
            problems.append({"field": "verdict", "expected": "UNCHANGED", "actual": verdict})

    if problems:
        status = "MISMATCHED"
    elif missing:
        status = "INCOMPLETE"
    else:
        status = "MATCHED"
    return {
        "status": status,
        "document_sha256": document_sha,
        "missing": missing,
        "mismatches": problems,
    }


def _read_evidence_file(evidence_dir: Path, relative: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Bytes of one evidence file, or ``(None, reason)``. Symlinks are refused."""
    candidate = evidence_dir / relative
    if candidate.is_symlink():
        return None, f"{relative} is a symlink; evidence symlinks are refused, not followed"
    if not candidate.is_file():
        return None, f"{relative} is missing from {evidence_dir}"
    try:
        return candidate.read_bytes(), None
    except OSError as exc:
        return None, f"{relative} is unreadable: {exc}"


def verify_immutable_source_section(
    source: Mapping[str, Any],
    *,
    evidence_model: str,
    expected_source_identity: Optional[SourceIdentity],
    evidence_dir: Path,
) -> Optional[str]:
    """Validate the ``source`` provenance of immutable-source live evidence.

    Returns ``None`` when every mandatory fact is present and agrees, else a
    one-line reason. The rules (contract В§5):

    * no retired prepared-build / overlay field may be present at all;
    * ``gonka_sha`` and ``marketplace_commit_sha`` are full commit SHAs and
      equal the selected commits; the tree SHAs are present (and equal the
      expected trees when the plan knows them);
    * ``source_immutability_verdict`` is exactly ``UNCHANGED`` and
      ``source-immutability.json`` beside the live context agrees with it;
    * every declared evidence hash is present and re-computes from the file;
    * the running binary reports exactly the selected Gonka commit.

    An expected identity that itself names a prepared or overlay tree cannot
    grade this evidence: it describes a historical run.
    """
    for field_name in RETIRED_PREPARED_SOURCE_FIELDS:
        if field_name in source:
            return (
                f"live-context.json 'source' carries {field_name!r}, a field of the retired "
                "prepared-build model; immutable-source evidence must not name any tree "
                "other than the selected commit"
            )

    m_commit = source.get("marketplace_commit_sha")
    if not _is_full_git_sha(m_commit):
        return (
            "live-context.json 'source' missing valid marketplace_commit_sha "
            f"(a full 40-hex commit SHA is mandatory, got {m_commit!r})"
        )
    g_commit = source.get("gonka_sha")
    if not _is_full_git_sha(g_commit):
        return (
            "live-context.json 'source' missing valid gonka_sha "
            f"(a full 40-hex commit SHA is mandatory, got {g_commit!r})"
        )
    if "gonka_source_sha" in source and source.get("gonka_source_sha") != g_commit:
        g_source = source.get("gonka_source_sha")
        expected_g = (
            expected_source_identity.gonka_commit_sha
            if expected_source_identity and expected_source_identity.gonka_commit_sha
            else g_commit
        )
        return (
            "Gonka source commit SHA mismatch in live-context.json: expected "
            f"{expected_g!r}, got {g_source!r}"
        )
    for tree_field in ("gonka_tree_sha", "marketplace_tree_sha"):
        if not _is_full_git_sha(source.get(tree_field)):
            return (
                f"live-context.json 'source' missing valid {tree_field} "
                f"(got {source.get(tree_field)!r})"
            )

    expected_gonka = expected_source_identity.gonka_commit_sha if expected_source_identity else None
    expected_market = (
        expected_source_identity.marketplace_commit_sha if expected_source_identity else None
    )

    if expected_source_identity is not None:
        if expected_source_identity.is_historical:
            return (
                "Expected source identity names a prepared or overlay tree; it describes a "
                "historical run and cannot grade immutable-source evidence"
            )
        if expected_market and m_commit != expected_market:
            return (
                "Marketplace commit SHA mismatch in live-context.json: expected "
                f"{expected_market!r}, got {m_commit!r}"
            )
        if expected_gonka and g_commit != expected_gonka:
            return (
                "Gonka source commit SHA mismatch in live-context.json: expected "
                f"{expected_gonka!r}, got {g_commit!r}"
            )
        for tree_field in ("gonka_tree_sha", "marketplace_tree_sha"):
            expected_tree = getattr(expected_source_identity, tree_field)
            if expected_tree and source.get(tree_field) != expected_tree:
                return (
                    f"{tree_field} mismatch in live-context.json: expected "
                    f"{expected_tree!r}, got {source.get(tree_field)!r}"
                )

    if requires_observed_selected_commit(evidence_model):
        runtime_section = source.get("runtime")
        if not isinstance(runtime_section, dict) or not runtime_section:
            return (
                "live-context.json 'source' missing mandatory 'runtime' identity "
                f"(expected the selected commit {g_commit!r} to be reported by the running binary)"
            )
        observed_commit = runtime_section.get("gonka_source_sha")
        if not isinstance(observed_commit, str) or not observed_commit.strip():
            return (
                "live-context.json 'source.runtime' missing mandatory gonka_source_sha "
                f"(expected the selected commit {g_commit!r})"
            )
        if observed_commit != g_commit:
            return (
                "Observed runtime commit mismatch in live-context.json: the running "
                f"binary reports {observed_commit!r}, but the selected commit is {g_commit!r}"
            )

    verdict = source.get("source_immutability_verdict")
    if verdict is None:
        return "live-context.json 'source' missing mandatory source_immutability_verdict"
    if verdict != "UNCHANGED":
        return (
            "Source immutability verdict in live-context.json is "
            f"{verdict!r}, expected 'UNCHANGED'; a changed or unmeasured source "
            "snapshot can never pass"
        )

    for field_name, relative in IMMUTABLE_SOURCE_HASHED_FILES:
        declared = source.get(field_name)
        if not isinstance(declared, str) or not _SHA256_HEX.match(declared.lower()):
            return f"live-context.json 'source' missing valid {field_name} (got {declared!r})"
        payload, reason = _read_evidence_file(evidence_dir, relative)
        if payload is None:
            return f"live-context.json declares {field_name} but {reason}"
        actual = hashlib.sha256(payload).hexdigest()
        if actual != declared.lower():
            return (
                f"{relative} SHA-256 mismatch: live-context.json declares {declared!r}, "
                f"the file hashes to {actual!r}"
            )

    document, _ = _read_evidence_file(evidence_dir, SOURCE_IMMUTABILITY_FILENAME)
    network_manifest, _ = _read_evidence_file(evidence_dir, "network/network-manifest.json")
    immutability = check_source_immutability_document(
        document,
        {"source": source},
        gonka_sha=expected_gonka or g_commit,
        marketplace_sha=expected_market or m_commit,
        network_manifest=network_manifest,
    )
    if immutability["status"] != "MATCHED":
        return (
            f"{SOURCE_IMMUTABILITY_FILENAME} is {immutability['status']}: "
            f"missing={immutability['missing']} mismatches={immutability['mismatches']}"
        )
    return None


def verify_live_context(
    context_path: Path,
    expected_checkpoints: Sequence[str] = (),
    expected_run_id: Optional[str] = None,
    expected_source_identity: Optional[SourceIdentity] = None,
    scenario_selector: Optional[str] = None,
    evidence_scopes: Sequence[str] = (),
    e2e_evidence_expected: bool = False,
    raw_bytes: Optional[bytes] = None,
) -> Tuple[bool, List[str], Optional[str]]:
    """Strictly validates live-context.json schema, provenance metadata, and checkpoints.

    ``e2e_evidence_expected`` says that this file belongs to an E2E run package
    (there is a run lock or an ``e2e-context.json`` beside it). It does not
    relax anything: it makes the E2E rules mandatory, so evidence inside such a
    package cannot be graded by the legacy rules by leaving a field out.

    Returns: (is_valid, observed_checkpoints, error_detail)
    """
    if not context_path.is_file():
        return False, [], f"live-context.json not found at: {context_path}"

    try:
        raw_text = (
            raw_bytes.decode("utf-8")
            if raw_bytes is not None else context_path.read_text(encoding="utf-8")
        )
        if not raw_text.strip():
            return False, [], "live-context.json is empty"
        data = json.loads(raw_text)
    except Exception as exc:
        return False, [], f"Malformed live-context.json (not valid JSON): {exc}"

    if not isinstance(data, dict):
        return False, [], "live-context.json root must be a JSON object"

    if data.get("test_fixture_only") is True:
        return False, [], "test-only fixture cannot be verified as live evidence"

    token_error = verify_local_token_evidence(data)
    if token_error is not None:
        return False, [], token_error

    # 1. Run ID validation
    actual_run_id = data.get("run_id")
    if expected_run_id is not None:
        if not actual_run_id:
            return False, [], f"live-context.json missing required 'run_id' (expected {expected_run_id!r})"
        if actual_run_id != expected_run_id:
            return (
                False,
                [],
                f"live-context.json run_id {actual_run_id!r} does not match expected {expected_run_id!r}",
            )

    # 2. Strict provenance validation: check 'source' dictionary
    source = data.get("source")
    if source is None or not isinstance(source, dict) or not source:
        return False, [], "live-context.json missing non-empty 'source' provenance dictionary"

    # Which rules produced this evidence is decided before anything is compared,
    # and independently of what is expected. Resolving it later -- nested under
    # "an expected identity was supplied" -- would mean that evidence carrying
    # no declaration never reached the anti-downgrade check, which is exactly
    # the field an attacker or a broken producer would leave out. Historical
    # (prepared-build, legacy) declarations are recognised and refused here:
    # they can be read, never graded as immutable-source proof.
    try:
        evidence_model = resolve_evidence_model(
            declared=declared_evidence_model(source),
        )
    except EvidenceModelError as exc:
        return False, [], f"live-context.json {exc}"

    source_error = verify_immutable_source_section(
        source,
        evidence_model=evidence_model,
        expected_source_identity=expected_source_identity,
        evidence_dir=context_path.parent,
    )
    if source_error is not None:
        return False, [], source_error

    # Check contract wasm references
    a9_manifest = source.get("a9_manifest_sha256")
    if a9_manifest is not None and (not isinstance(a9_manifest, str) or len(a9_manifest) != 64):
        return False, [], f"Invalid a9_manifest_sha256 in live-context.json: {a9_manifest!r}"

    # 3. Declared producer scopes must really exist before anything is credited
    scope_error = validate_evidence_scopes(data, evidence_scopes)
    if scope_error is not None:
        return False, [], f"live-context.json scope mismatch: {scope_error}"

    # 4. Checkpoint validation
    observed = extract_and_validate_scenario_predicates(
        data, scenario_selector, evidence_scopes
    )
    if source.get("settlement_token_mode") == MAINNET_MODE and scenario_selector == "terminal-release-repeat":
        if not _validate_mainnet_usdt_settlement(data) or not _validate_funded_release_lifecycle(data):
            return False, sorted(observed), "mainnet USDT settlement, repeated withdrawals or native release lifecycle is unproven"

    missing_cp = [cp for cp in expected_checkpoints if cp not in observed]
    if missing_cp:
        return (
            False,
            sorted(observed),
            f"Missing required checkpoints in live-context.json: {missing_cp}",
        )

    return True, sorted(observed), None


def verify_go_boundary_report(
    report_path: Path,
    raw_test_json: Optional[Path] = None,
    *,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[bool, List[str], Optional[str]]:
    """Strictly verifies Go boundary report and individual test results in raw/go-test.json."""
    if not report_path.is_file():
        return False, [], f"Go boundary report.json not found: {report_path}"
    try:
        report_bytes = artifact_reader(report_path) if artifact_reader else report_path.read_bytes()
        rep = json.loads(report_bytes.decode("utf-8"))
    except Exception as exc:
        return False, [], f"Corrupted report.json: {exc}"

    if not isinstance(rep, dict):
        return False, [], "Go boundary report.json root must be a JSON object"

    # The committed test fixtures carry this marker precisely so that a copy of
    # one can never be graded as a run's evidence. verify_live_context refuses
    # it for the live context; the boundary reports must not be the one door
    # that check leaves open, or a fixture report dropped into a run directory
    # would pass as a real Go boundary result.
    if rep.get("test_fixture_only") is True:
        return False, [], "test-only fixture cannot be verified as live evidence"

    if rep.get("status") != "PASS":
        return False, [], f"Go boundary status is {rep.get('status')!r}, expected 'PASS'"
    if str(rep.get("docker_exit")) != "0":
        return False, [], f"Go boundary docker_exit is {rep.get('docker_exit')!r}"
    if str(rep.get("go_exit")) != "0":
        return False, [], f"Go boundary go_exit is {rep.get('go_exit')!r}"

    # A passing summary cannot substitute for the individual Go test events.
    test_json_candidates = [
        raw_test_json,
        report_path.parent / "raw" / "go-test.json",
        report_path.parent / "go-test.json",
    ]
    actual_test_json = next((p for p in test_json_candidates if p and p.is_file()), None)
    if actual_test_json is None:
        return False, [], "Go boundary raw/go-test.json is missing"
    try:
        test_bytes = artifact_reader(actual_test_json) if artifact_reader else actual_test_json.read_bytes()
        recorded_hash = rep.get("test_output_sha256")
        if not isinstance(recorded_hash, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_hash) is None:
            return False, [], "Go boundary report has invalid test_output_sha256"
        if hashlib.sha256(test_bytes).hexdigest() != recorded_hash:
            return False, [], "Go boundary test_output_sha256 differs from raw/go-test.json"
        lines = test_bytes.decode("utf-8", errors="replace").splitlines()
        events = [json.loads(line) for line in lines if line.strip().startswith("{")]
        passed_names = {e.get("Test") for e in events if e.get("Action") == "pass" and e.get("Test")}
        missing = GO_BOUNDARY_REQUIRED_TESTS - passed_names
        if missing:
            return False, sorted(passed_names), f"Go boundary missing passed tests: {sorted(missing)}"
        return True, sorted(passed_names & GO_BOUNDARY_REQUIRED_TESTS), None
    except Exception as exc:
        return False, [], f"Failed to parse raw/go-test.json: {exc}"


def verify_wasm_abi_report(
    abi_json_path: Path,
    wasm_path: Path,
    *,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[bool, List[str], Optional[str]]:
    """Verify the 9-case ABI report against the exact saved Wasm artifact."""
    if not abi_json_path.is_file():
        return False, [], f"Wasm ABI report not found: {abi_json_path}"
    reader = artifact_reader or read_checked_file_bytes
    try:
        abi_bytes = reader(abi_json_path)
        data = json.loads(abi_bytes.decode("utf-8"))
    except Exception as exc:
        return False, [], f"Corrupted abi.json: {exc}"
    if not isinstance(data, dict):
        return False, [], "Wasm ABI report is not an object"

    # Same refusal as verify_live_context and verify_go_boundary_report: the
    # committed Wasm fixture is a test input, never a probe result, and the
    # marker is checked before a single case is credited.
    if data.get("test_fixture_only") is True:
        return False, [], "test-only fixture cannot be verified as live evidence"

    if data.get("status") != "PASS":
        return False, [], f"Wasm ABI probe status is {data.get('status')!r}, expected 'PASS'"

    cases = data.get("cases", [])
    if not isinstance(cases, list):
        return False, [], "Wasm ABI 'cases' is not a list"

    if len(cases) != 9:
        return False, [], f"Wasm ABI probe evaluated {len(cases)} cases, expected exactly 9"

    passed_case_names: Set[str] = set()
    for c in cases:
        if not isinstance(c, dict):
            return False, sorted(passed_case_names), "Wasm ABI case is not an object"
        c_name = c.get("name")
        c_stat = c.get("status")
        if c_stat != "PASS":
            return False, sorted(passed_case_names), f"Wasm ABI case {c_name} did not pass: {c_stat}"
        if c_name:
            passed_case_names.add(c_name)

    missing_cases = WASM_ABI_REQUIRED_CASES - passed_case_names
    if missing_cases:
        return (
            False,
            sorted(passed_case_names),
            f"Wasm ABI missing expected case IDs: {sorted(missing_cases)}",
        )

    reported_hash = data.get("wasm_sha256")
    if not isinstance(reported_hash, str) or re.fullmatch(r"[0-9a-f]{64}", reported_hash) is None:
        return False, sorted(passed_case_names), "Wasm ABI report has invalid wasm_sha256"
    if not wasm_path.is_file():
        return False, sorted(passed_case_names), f"Wasm ABI artifact not found: {wasm_path}"
    try:
        actual_hash = hashlib.sha256(reader(wasm_path)).hexdigest()
    except Exception as exc:
        return False, sorted(passed_case_names), f"Wasm ABI artifact could not be read: {exc}"
    if actual_hash != reported_hash:
        return False, sorted(passed_case_names), "Wasm hash mismatch with ABI report"

    return True, sorted(passed_case_names), None


def verify_cargo_test_log(
    log_path: Path,
    expected_test: Optional[str] = None,
    *,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[bool, int, Optional[str]]:
    """Parse cargo test log to ensure tests actually executed and passed (0 tests is a failure)."""
    if not log_path.is_file():
        return False, 0, f"Cargo test log not found: {log_path}"

    try:
        log_bytes = artifact_reader(log_path) if artifact_reader else log_path.read_bytes()
        content = log_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        return False, 0, f"Cannot safely read Cargo test log: {exc}"
    if expected_test and not re.search(
        r"^test (?:[A-Za-z0-9_]+::)*" + re.escape(expected_test) + r" \.\.\. ok\s*$",
        content, re.MULTILINE,
    ):
        return False, 0, f"Required Cargo test did not pass: {expected_test}"
    # Search for: test result: ok. X passed; 0 failed; 0 ignored
    match = re.search(r"test result:\s+ok\.\s+(\d+)\s+passed;\s+(\d+)\s+failed;\s+(\d+)\s+ignored", content)
    if not match:
        if "0 passed; 0 failed" in content:
            return False, 0, "Cargo test executed 0 tests (filter matched nothing)"
        return False, 0, "No successful cargo test summary found in log"

    passed_count = int(match.group(1))
    failed_count = int(match.group(2))

    if failed_count > 0:
        return False, passed_count, f"{failed_count} tests failed in cargo test"
    if passed_count == 0:
        return False, 0, "Zero tests executed in cargo test (0 passed)"

    return True, passed_count, None


def verify_package_c_policy_log(
    log_path: Path,
    *,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[bool, List[str], Optional[str]]:
    """Require every replacement test and every historical case marker exactly once."""
    if not log_path.is_file():
        return False, [], f"Cargo test log not found: {log_path}"
    try:
        log_bytes = artifact_reader(log_path) if artifact_reader else log_path.read_bytes()
        content = log_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        return False, [], f"Cannot safely read Package C policy log: {exc}"
    if not content.strip():
        return False, [], "Package C policy log is empty"

    # --test-threads=1 binds a standalone completion to the active test.
    # --nocapture can put stdout after "...", so completion need not be on
    # the starting line. Aggregate counts cannot substitute for that completion.
    passed_tests: Set[str] = set()
    started_tests: Set[str] = set()
    active_test: Optional[str] = None
    for line in content.splitlines():
        start = re.match(r"^test ([A-Za-z0-9_:]+) \.\.\.\s*(.*)$", line)
        if start:
            if active_test is not None:
                return False, [], f"Cargo test did not finish: {active_test}"
            active_test = start.group(1)
            if active_test in started_tests:
                return False, [], f"Duplicate Cargo test: {active_test}"
            started_tests.add(active_test)
            line = start.group(2)
        if line.startswith("test result:") and active_test is not None:
            return False, [], f"Cargo summary before test completion: {active_test}"
        if re.match(r"^test result:\s+FAILED\.", line):
            return False, [], "Cargo reported a failed test suite"
        if active_test is not None:
            outcome = line.strip()
            if outcome == "ok":
                if active_test.split("::")[-1].startswith("c_"):
                    passed_tests.add(active_test)
                active_test = None
            elif outcome == "FAILED" or re.match(r"^ignored(?:$|[,\s])", outcome):
                return False, [], f"Cargo test did not pass: {active_test}: {outcome}"
    if active_test is not None:
        return False, [], f"Cargo test did not finish: {active_test}"
    missing_tests = sorted(PACKAGE_C_POLICY_TESTS - passed_tests)
    foreign_tests = sorted(passed_tests - PACKAGE_C_POLICY_TESTS)
    if missing_tests or foreign_tests:
        return (
            False,
            [],
            f"Package C policy test mismatch; missing={missing_tests}, foreign={foreign_tests}",
        )

    # The first marker from a nocapture test can share Cargo's ``test ...`` line.
    markers = re.findall(r"C_CASE:([^\s]+)", content)
    counts = Counter(markers)
    observed = set(counts)
    missing_cases = sorted(PACKAGE_C_POLICY_CASES - observed)
    foreign_cases = sorted(observed - PACKAGE_C_POLICY_CASES)
    duplicates = sorted(case for case, count in counts.items() if count != 1)
    if missing_cases or foreign_cases or duplicates:
        return (
            False,
            sorted(observed),
            "Package C policy marker mismatch; "
            f"missing={missing_cases}, foreign={foreign_cases}, duplicates={duplicates}",
        )

    summaries = re.findall(
        r"test result:\s+ok\.\s+(\d+)\s+passed;\s+0\s+failed;", content
    )
    if not summaries or sum(int(value) for value in summaries) < len(PACKAGE_C_POLICY_TESTS):
        return False, sorted(observed), "Cargo summaries do not prove all 18 replacement tests"
    return True, sorted(observed), None


#: Exact upstream Rust test each contract-test task must prove, keyed by the
#: canonical task ID. Old task IDs are not translated.
CONTRACT_BOUNDARY_TESTS = {
    "contract-network-unconfirmed-policy": (
        "network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
        "test_network_unconfirmed_refund_passed",
    ),
    "contract-claim-expiry-policy": (
        "claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
        "test_claim_expiry_checks_passed",
    ),
}


def boundary_verdict(task, ok, observed, checkpoints, missing_artifacts, error):
    """Credit only known predicates proved by the successful boundary verifier."""
    observed = sorted(set(observed) | (set(checkpoints) if ok else set()))
    missing = sorted(set(task.expected_checkpoints) - set(observed))
    if not ok or missing:
        return ExecutionStatus.FAILED, EvidenceStatus.INCOMPLETE, observed, missing_artifacts, error or f"Missing checkpoints: {missing}"
    return (ExecutionStatus.PASSED,
            EvidenceStatus.INCOMPLETE if missing_artifacts else EvidenceStatus.COMPLETE,
            observed, missing_artifacts,
            f"Missing expected artifacts: {missing_artifacts}" if missing_artifacts else None)


def evaluate_task_evidence(
    task: TaskPlan,
    evidence_dir: Path,
    junit_dir: Path,
    raw_status: ExecutionStatus,
    exit_code: Optional[int],
    run_id: Optional[str] = None,
    source_identity: Optional[SourceIdentity] = None,
    e2e_evidence_expected: bool = False,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
) -> Tuple[ExecutionStatus, EvidenceStatus, List[str], List[str], Optional[str]]:
    """Evaluates task evidence completeness and determines final verified execution and evidence status.

    Fail-closed:
    - Never awards COMPLETE if any expected artifact is missing.
    - Automated native PASSED requires exit 0, exact JUnit match without skip/fail, and valid live-context.
    - Package C requires exact 73 cases, PASS on each row, and verified activation sequence.
    - Historical C timeout or missing JUnit remains TIMED_OUT / FAILED.
    - ``e2e_evidence_expected`` makes the E2E evidence model mandatory for this
      task's live context. It is set by the E2E runner and never by a legacy
      run, which is what keeps the two sets of rules apart without either of
      them being optional.

    Returns: (final_execution_status, evidence_status, observed_cases, missing_artifacts, error_detail)
    """
    missing_artifacts: List[str] = []
    observed_cases: List[str] = []

    # Check for expected artifacts across possible directory layouts (direct vs evidence/<run_id>/).
    # The names come from the package's own suite plan, so an older package asks
    # for its own older file names; no alternative spellings are tried here.
    for art in task.expected_artifacts:
        if art.startswith("junit/"):
            art_path = junit_dir / Path(art).name
        else:
            candidates = [
                evidence_dir / art,
                evidence_dir / (run_id or "") / art,
                junit_dir.parent / art,
            ]
            art_path = next((p for p in candidates if p.exists()), candidates[0])

        if not art_path.exists():
            missing_artifacts.append(art)

    if task.proof_level == ProofLevel.NATIVE:
        # Check JUnit XML
        junit_files = list(junit_dir.glob("TEST-*.xml")) if junit_dir.exists() else []
        if not junit_files:
            if "junit/TEST-MarketplaceContractAcceptanceTests.xml" not in missing_artifacts:
                missing_artifacts.append("junit/TEST-MarketplaceContractAcceptanceTests.xml")
            if raw_status == ExecutionStatus.PASSED:
                return (
                    ExecutionStatus.FAILED,
                    EvidenceStatus.INCOMPLETE,
                    [],
                    missing_artifacts,
                    "No JUnit XML found for native scenario; automated exit 0 is insufficient",
                )

        # Parse JUnit with exact test method matching
        junit_passed = False
        junit_err: Optional[str] = None
        expected_method = task.exact_test_method or task.scenario_selector

        for jf in junit_files:
            passed, obs, err = verify_junit_xml(
                jf,
                expected_method=expected_method,
                exact_match=bool(task.exact_test_method),
                artifact_reader=artifact_reader,
            )
            observed_cases.extend(obs)
            if passed:
                junit_passed = True
                break
            junit_err = err

        if not junit_passed:
            return (
                ExecutionStatus.FAILED,
                EvidenceStatus.INCOMPLETE,
                observed_cases,
                missing_artifacts,
                junit_err or "JUnit test did not pass",
            )

        # Validate live-context.json provenance and checkpoints
        context_candidates = [
            evidence_dir / "live-context.json",
            evidence_dir / (run_id or "") / "live-context.json",
        ]
        context_path = next((p for p in context_candidates if p.exists()), context_candidates[0])
        try:
            context_bytes = artifact_reader(context_path) if artifact_reader is not None else None
        except Exception as exc:
            return ExecutionStatus.FAILED, EvidenceStatus.INCOMPLETE, observed_cases, missing_artifacts, (
                f"Cannot safely read live-context.json: {exc}"
            )
        ctx_ok, ctx_obs, ctx_err = verify_live_context(
            context_path=context_path,
            expected_checkpoints=task.expected_checkpoints,
            expected_run_id=run_id,
            expected_source_identity=source_identity,
            scenario_selector=task.scenario_selector,
            evidence_scopes=task.evidence_scopes,
            e2e_evidence_expected=e2e_evidence_expected,
            raw_bytes=context_bytes,
        )
        if not ctx_ok:
            return (
                ExecutionStatus.FAILED,
                EvidenceStatus.INCOMPLETE,
                observed_cases,
                missing_artifacts,
                ctx_err,
            )

        if raw_status == ExecutionStatus.PASSED and not missing_artifacts:
            return ExecutionStatus.PASSED, EvidenceStatus.COMPLETE, sorted(set(observed_cases) | set(ctx_obs)), [], None
        elif raw_status == ExecutionStatus.PASSED:
            return (
                ExecutionStatus.PASSED,
                EvidenceStatus.INCOMPLETE,
                observed_cases,
                missing_artifacts,
                f"Missing expected artifacts: {missing_artifacts}",
            )
        else:
            return raw_status, EvidenceStatus.INCOMPLETE, observed_cases, missing_artifacts, "Execution failed"

    elif task.proof_level == ProofLevel.GO_BOUNDARY:
        report_candidates = [
            evidence_dir / "go-boundary" / "report.json",
            evidence_dir / "report.json",
            evidence_dir / (run_id or "") / "report.json",
        ]
        report_path = next((p for p in report_candidates if p.exists()), report_candidates[0])
        ok, obs_tests, err = verify_go_boundary_report(
            report_path, artifact_reader=artifact_reader,
        )
        return boundary_verdict(task, ok and raw_status == ExecutionStatus.PASSED and exit_code == 0,
                                obs_tests, ["docker_build_completed", "go_exit_zero",
                                            *[name + "_passed" for name in obs_tests]], missing_artifacts, err)

    elif task.proof_level == ProofLevel.WASM_ABI:
        abi_candidates = [
            evidence_dir / "abi.json",
            evidence_dir / (run_id or "") / "abi.json",
        ]
        abi_path = next((p for p in abi_candidates if p.exists()), abi_candidates[0])
        wasm_candidates = [
            evidence_dir / "a8_query_boundary.wasm",
            evidence_dir / (run_id or "") / "a8_query_boundary.wasm",
        ]
        wasm_path = next((p for p in wasm_candidates if p.exists()), wasm_candidates[0])
        ok, obs_cases, err = verify_wasm_abi_report(
            abi_path, wasm_path, artifact_reader=artifact_reader,
        )
        return boundary_verdict(task, ok and raw_status == ExecutionStatus.PASSED and exit_code == 0,
                                obs_cases, ["wasm_compiled", "9_abi_cases_passed", "status_pass"], missing_artifacts, err)

    elif task.proof_level == ProofLevel.CONTRACT_TEST:
        # The log is named after the task ID recorded in the package's own
        # suite plan, so an older package looks for its older name on its own;
        # no alternative spellings are tried.
        log_candidates = [
            evidence_dir / f"{task.task_id}.log",
            evidence_dir / (run_id or "") / f"{task.task_id}.log",
        ]
        log_file = next((p for p in log_candidates if p.exists()), log_candidates[0])
        contract_task_id = task.task_id
        if contract_task_id == "contract-query-fault-policy":
            ok, observed, err = verify_package_c_policy_log(
                log_file, artifact_reader=artifact_reader,
            )
            return boundary_verdict(
                task,
                ok and raw_status == ExecutionStatus.PASSED and exit_code == 0,
                observed,
                [
                    "cargo_test_exit_zero",
                    "18_exact_tests_passed",
                    "71_case_markers_passed",
                ],
                missing_artifacts,
                err,
            )
        expected = CONTRACT_BOUNDARY_TESTS.get(contract_task_id)
        if expected is None:
            return ExecutionStatus.FAILED, EvidenceStatus.INCOMPLETE, [], missing_artifacts, "Unknown contract boundary test"
        ok, count, err = verify_cargo_test_log(
            log_file, expected[0], artifact_reader=artifact_reader,
        )
        return boundary_verdict(task, ok and raw_status == ExecutionStatus.PASSED and exit_code == 0,
                                [expected[0]] if ok else [],
                                ["cargo_test_exit_zero", "non_zero_tests_executed", expected[1]], missing_artifacts, err)

    return raw_status, EvidenceStatus.INCOMPLETE, [], missing_artifacts, None


# ---------------------------------------------------------------------------
# Suite-level evidence verification and recalculation
# ---------------------------------------------------------------------------

# Statuses that prove a task ran to completion and therefore must own a
# run directory with its mandatory artifacts.
COMPLETED_EXECUTION_STATUSES = (
    ExecutionStatus.PASSED,
    ExecutionStatus.FAILED,
    ExecutionStatus.TIMED_OUT,
)

# Journal events that mark the end of an execution attempt.
COMPLETION_EVENT_TYPES = frozenset(
    {"ADAPTER_COMPLETED", "ADAPTER_FAILED", "TASK_COMPLETED", "TASK_FAILED", "TASK_EXCEPTION"}
)


@dataclass(frozen=True)
class SuiteVerification:
    """Explicit result of verifying and recalculating a suite directory."""

    suite_dir: Path
    plan: Optional[SuitePlan]
    stored_result: Optional[SuiteResult]
    result: SuiteResult
    integrity_errors: Tuple[str, ...]
    valid_artifacts: int
    total_artifacts: int


def build_task_source_identity(
    plan: Optional[SuitePlan],
    identity_data: Optional[Mapping[str, Any]] = None,
) -> Optional[SourceIdentity]:
    """Builds the identity of one specific runtime snapshot.

    The suite-level identity does not know the per-run snapshot facts, so the
    run's own identity.json wins.

    Under the immutable-source runner the selected Gonka commit is the commit
    the chain binary was compiled from, and the running binary must report it
    exactly; the tree SHAs pin the Git trees that were materialised.

    Historical prepared/overlay fields are carried through unchanged when an
    old identity.json names them, so that such a run is recognisable as
    historical (``SourceIdentity.is_historical``) and is refused by the
    immutable-source grading instead of being silently read as current.
    """
    base = plan.source_identity if plan else None
    identity_data = identity_data or {}
    if base is None and not identity_data:
        return None

    return SourceIdentity(
        marketplace_commit_sha=identity_data.get("marketplace_commit_sha")
        or (base.marketplace_commit_sha if base else ""),
        gonka_commit_sha=identity_data.get("gonka_source_sha")
        or identity_data.get("gonka_commit_sha")
        or (base.gonka_commit_sha if base else ""),
        runner_version_hash=base.runner_version_hash if base else "",
        catalog_version_hash=base.catalog_version_hash if base else "",
        gonka_tree_sha=identity_data.get("gonka_tree_sha")
        or (base.gonka_tree_sha if base else None),
        marketplace_tree_sha=identity_data.get("marketplace_tree_sha")
        or (base.marketplace_tree_sha if base else None),
        source_immutability_verdict=base.source_immutability_verdict if base else None,
        gonka_overlay_manifest_sha256=identity_data.get("gonka_overlay_manifest_sha256")
        or (base.gonka_overlay_manifest_sha256 if base else None),
        gonka_prepared_sha=identity_data.get("gonka_prepared_sha")
        or (base.gonka_prepared_sha if base else None),
    )


def executed_suite_task_ids(
    suite_dir: Path,
    result: Optional[SuiteResult] = None,
) -> Set[str]:
    """Returns task IDs that ran to completion, from the result and journal.

    NOT_RUN and CANCELLED tasks never create a run directory, and an
    INTERRUPTED task may have been stopped before its runtime snapshot
    existed, so neither is required to own one.
    """
    suite_root = Path(suite_dir).resolve()
    events_file = suite_root / "events.jsonl"
    executed: Set[str] = set()
    described: Set[str] = set()

    if result is not None:
        for task_result in result.tasks:
            described.add(task_result.task_id)
            if task_result.execution_status in COMPLETED_EXECUTION_STATUSES:
                executed.add(task_result.task_id)

    # Journal fallback for tasks the result does not describe at all.
    if events_file.is_file():
        try:
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                task_id = event.get("task_id")
                if (
                    task_id
                    and task_id not in described
                    and event.get("event_type") in COMPLETION_EVENT_TYPES
                ):
                    executed.add(task_id)
        except Exception:
            pass

    return executed


def _load_suite_plan_and_results(
    suite_dir: Path,
    *,
    suite_plan_bytes: Optional[bytes] = None,
    suite_result_bytes: Optional[bytes] = None,
    suite_result_checked: bool = False,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
    parsed_result_hashes: Optional[Dict[str, str]] = None,
) -> Tuple[SuitePlan, Optional[SuiteResult], SuiteResult]:
    """Load suite-plan.json and return (plan, stored_result, working_result).

    ``stored_result`` is the unmodified ``SuiteResult`` parsed from ``suite-result.json``
    when present and structurally consistent with the plan; ``working_result`` has its
    per-task entries refreshed from ``runs/<task_run_id>/result.json`` or reconstructed
    from the journal if ``suite-result.json`` is absent or unreadable.
    """
    suite_root = Path(suite_dir).resolve()
    if not suite_root.is_dir():
        raise FileNotFoundError(f"Suite directory not found: {suite_root}")
    plan_file = suite_root / "suite-plan.json"
    result_file = suite_root / "suite-result.json"
    events_file = suite_root / "events.jsonl"

    def read_task_result(path: Path) -> TaskResult:
        raw = artifact_reader(path) if artifact_reader is not None else path.read_bytes()
        if parsed_result_hashes is not None:
            parsed_result_hashes[path.relative_to(suite_root).as_posix()] = hashlib.sha256(raw).hexdigest()
        return TaskResult.from_dict(json.loads(raw.decode("utf-8")))

    if suite_plan_bytes is None and not plan_file.is_file():
        raise FileNotFoundError(f"Missing suite plan file: {plan_file}")

    plan_raw = suite_plan_bytes if suite_plan_bytes is not None else plan_file.read_bytes()
    plan_data = json.loads(plan_raw.decode("utf-8"))
    schema_ver = plan_data.get("schema_version")
    if schema_ver not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"Unsupported plan schema_version: {schema_ver!r}")
    plan = SuitePlan.from_dict(plan_data)

    if suite_result_bytes is not None or (not suite_result_checked and result_file.is_file()):
        try:
            result_raw = suite_result_bytes if suite_result_bytes is not None else result_file.read_bytes()
            res_data = json.loads(result_raw.decode("utf-8"))
            res_schema = res_data.get("schema_version")
            if res_schema in SUPPORTED_SCHEMA_VERSIONS:
                stored_obj = SuiteResult.from_dict(res_data)
                res_obj = SuiteResult.from_dict(res_data)
                if res_obj.suite_id == plan.suite_id:
                    plan_tids = [t.task_id for t in plan.tasks]
                    res_tids = [t.task_id for t in res_obj.tasks]
                    if plan_tids == res_tids:
                        for i, t in enumerate(res_obj.tasks):
                            task_plan = next((tp for tp in plan.tasks if tp.task_id == t.task_id), None)
                            if task_plan:
                                task_run_id = make_task_run_id(plan.suite_id, task_plan.ordinal, task_plan.task_id)
                                tr_file = suite_root / "runs" / task_run_id / "result.json"
                                if tr_file.is_file():
                                    try:
                                        tr_disk = read_task_result(tr_file)
                                        res_obj.tasks[i] = tr_disk
                                    except Exception:
                                        pass
                        return plan, stored_obj, res_obj
        except Exception:
            pass  # Fall back to reconstruction

    tasks_results: List[TaskResult] = []
    overall = ExecutionStatus.PASSED

    started_tasks: set[str] = set()
    failed_tasks: set[str] = set()
    if events_file.is_file():
        try:
            for line in events_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                t_id = ev.get("task_id")
                ev_type = ev.get("event_type")
                if t_id:
                    started_tasks.add(t_id)
                if ev_type in ("ADAPTER_FAILED", "TASK_FAILED", "PREPARE_FAILED"):
                    if t_id:
                        failed_tasks.add(t_id)
        except Exception:
            pass

    for task_plan in plan.tasks:
        run_id = make_task_run_id(plan.suite_id, task_plan.ordinal, task_plan.task_id)
        run_result_file = suite_root / "runs" / run_id / "result.json"

        if run_result_file.is_file():
            try:
                tr = read_task_result(run_result_file)
                tasks_results.append(tr)
                if tr.execution_status != ExecutionStatus.PASSED:
                    overall = ExecutionStatus.FAILED
                continue
            except Exception:
                pass

        task_status = ExecutionStatus.NOT_RUN
        if task_plan.task_id in failed_tasks:
            task_status = ExecutionStatus.FAILED
            overall = ExecutionStatus.FAILED
        elif task_plan.task_id in started_tasks:
            task_status = ExecutionStatus.INTERRUPTED
            overall = ExecutionStatus.INTERRUPTED

        tr = TaskResult(
            task_id=task_plan.task_id,
            ordinal=task_plan.ordinal,
            run_id=run_id,
            proof_level=task_plan.proof_level,
            execution_status=task_status,
            evidence_status=EvidenceStatus.NOT_COLLECTED,
            cleanup_status=CleanupStatus.NOT_NEEDED,
            acceptance_status=AcceptanceStatus.NOT_REVIEWED,
            start_time_utc=None,
            end_time_utc=None,
            duration_seconds=None,
            phase="RECONSTRUCTED",
            exit_code=None,
            primary_failure="Suite stopped or interrupted before this task finished",
            secondary_errors=[],
            expected_cases=task_plan.expected_checkpoints,
            observed_passed_cases=[],
            missing_cases=task_plan.expected_checkpoints,
            missing_artifacts=task_plan.expected_artifacts,
            raw_evidence_dir=None,
            exported_evidence_dir=None,
        )
        tasks_results.append(tr)
        if overall == ExecutionStatus.PASSED:
            overall = ExecutionStatus.INTERRUPTED

    reconstructed = SuiteResult(
        schema_version="1.0.0",
        suite_id=plan.suite_id,
        created_at_utc=plan.created_at_utc,
        completed_at_utc=None,
        source_identity=plan.source_identity,
        overall_status=overall,
        tasks=tasks_results,
        summary_message="Reconstructed from suite artifacts and journal",
    )
    return plan, None, reconstructed


def load_or_reconstruct_suite_result(suite_dir: Path) -> SuiteResult:
    """Loads suite-result.json if valid and consistent; otherwise reconstructs from journal / runs."""
    _, _, result = _load_suite_plan_and_results(suite_dir)
    return result


def verify_suite_artifacts_integrity(
    suite_dir: Path,
    plan: Optional[SuitePlan] = None,
    result: Optional[SuiteResult] = None,
    *,
    e2e_evidence_expected: bool = True,
    artifact_index_bytes: Optional[bytes] = None,
    artifact_index_checked: bool = False,
    artifact_hasher: Optional[Callable[[Path], str]] = None,
    artifact_sizer: Optional[Callable[[Path], int]] = None,
    artifact_digester: Optional[Callable[[Path], Tuple[str, int]]] = None,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
    parsed_result_hashes: Optional[Mapping[str, str]] = None,
) -> Tuple[int, int, List[str]]:
    """Verifies on-disk artifacts against artifact-index.json with strict path boundary checks
    and enforces presence of mandatory artifacts, identities, and valid structured evidence.
    """
    if artifact_reader is None:
        artifact_reader = read_checked_file_bytes
    if artifact_digester is None:
        if artifact_hasher is None and artifact_sizer is None:
            artifact_digester = sha256_size_checked_file
        else:
            artifact_hasher = artifact_hasher or sha256_file
            artifact_sizer = artifact_sizer or (lambda path: path.stat().st_size)
    suite_root = Path(suite_dir).resolve()
    if not suite_root.is_dir():
        raise FileNotFoundError(f"Suite directory not found: {suite_root}")
    index_file = suite_root / "artifact-index.json"
    plan_file = suite_root / "suite-plan.json"

    if (artifact_index_bytes is None if artifact_index_checked else not index_file.is_file()):
        return 0, 0, ["artifact-index.json not found"]

    if plan is None and plan_file.is_file():
        try:
            plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
            plan = SuitePlan.from_dict(plan_data)
        except Exception:
            pass

    try:
        index_data = artifact_index_bytes if artifact_index_checked else index_file.read_bytes()
        data = json.loads(index_data.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
            raise ValueError("Expected an artifacts array")
        items = data["artifacts"]
    except Exception as exc:
        return 0, 0, [f"Corrupted artifact-index.json: {exc}"]

    valid_count = 0
    missing_or_corrupt: List[str] = []
    indexed_paths: Set[str] = set()
    declared_paths: Set[str] = set()
    indexed_hashes: Dict[str, str] = {}
    if data.get("schema_version") != "1.0.0":
        missing_or_corrupt.append("Unsupported or missing artifact index schema_version")
    if type(data.get("total_artifacts")) is not int or data["total_artifacts"] != len(items):
        missing_or_corrupt.append("Artifact index count does not match the artifacts array")

    def read_indexed_evidence(path: Path) -> bytes:
        """Bind parsed task evidence to the bytes accepted by this index."""
        if artifact_reader is None:
            return path.read_bytes()
        rel = path.relative_to(suite_root).as_posix()
        expected = indexed_hashes.get(rel)
        if expected is None:
            raise ValueError(f"Task evidence {rel} has no verified indexed hash")
        data = artifact_reader(path)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"Task evidence {rel} differs from its indexed bytes")
        return data

    run_id_by_task: Dict[str, str] = {}
    task_by_run_id: Dict[str, str] = {}
    if plan:
        for task_plan in plan.tasks:
            task_run_id = make_task_run_id(plan.suite_id, task_plan.ordinal, task_plan.task_id)
            run_id_by_task[task_plan.task_id] = task_run_id
            task_by_run_id[task_run_id] = task_plan.task_id

    for item in items:
        try:
            rel = validate_artifact_index_record(item)
        except ValueError as exc:
            missing_or_corrupt.append(f"Invalid artifact entry: {exc}")
            continue
        if rel in declared_paths:
            missing_or_corrupt.append(f"Duplicate artifact path: {rel}")
            continue
        expected_sha = item["sha256"]
        declared_paths.add(rel)

        rel_parts = Path(rel).parts
        if rel_parts and rel_parts[0] == "runs":
            entry_dir = rel_parts[1] if len(rel_parts) > 1 else ""
            if plan and entry_dir not in task_by_run_id:
                missing_or_corrupt.append(
                    f"Artifact {rel} is indexed under run directory {entry_dir!r} "
                    "that this suite plan does not define"
                )
                continue
            entry_run_id = item.get("run_id")
            if plan and str(entry_run_id) != entry_dir:
                missing_or_corrupt.append(
                    f"Artifact {rel} records run_id {entry_run_id!r} "
                    f"but is stored under {entry_dir!r}"
                )
                continue
            entry_task_id = item.get("task_id")
            if plan and str(entry_task_id) != task_by_run_id[entry_dir]:
                missing_or_corrupt.append(
                    f"Artifact {rel} links task {entry_task_id!r} to run "
                    f"{entry_dir!r} instead of the planned task {task_by_run_id[entry_dir]!r}"
                )
                continue

        if rel.startswith("/") or rel.startswith("\\") or ".." in Path(rel).parts:
            missing_or_corrupt.append(f"Security: artifact path contains traversal or absolute escape: {rel}")
            continue

        file_path = suite_root / rel
        try:
            resolved = file_path.resolve()
            resolved.relative_to(suite_root)
        except (ValueError, RuntimeError):
            missing_or_corrupt.append(f"Security: artifact path escapes suite directory: {rel}")
            continue

        if not file_path.is_file():
            missing_or_corrupt.append(f"Missing file: {rel}")
            continue

        if file_path.stat().st_size == 0 and not rel.endswith(".log"):
            missing_or_corrupt.append(f"Empty artifact file: {rel}")
            continue

        if artifact_digester is None:
            try:
                actual_sha = artifact_hasher(file_path)
            except (OSError, RuntimeError) as exc:
                missing_or_corrupt.append(f"Cannot safely hash artifact {rel}: {exc}")
                continue
        else:
            try:
                actual_sha, checked_size = artifact_digester(file_path)
            except (OSError, RuntimeError, ValueError) as exc:
                missing_or_corrupt.append(f"Cannot safely hash and size artifact {rel}: {exc}")
                continue
        if actual_sha != expected_sha:
            missing_or_corrupt.append(f"Checksum mismatch for {rel}: expected {expected_sha}, got {actual_sha}")
            continue
        if artifact_digester is None:
            try:
                checked_size = artifact_sizer(file_path)
            except (OSError, RuntimeError, ValueError) as exc:
                missing_or_corrupt.append(f"Cannot safely size artifact {rel}: {exc}")
                continue
        if checked_size != item["size_bytes"]:
            missing_or_corrupt.append(
                f"Size mismatch for {rel}: expected {item['size_bytes']}, got {checked_size}"
            )
            continue
        if parsed_result_hashes is not None and rel in parsed_result_hashes:
            if parsed_result_hashes[rel] != actual_sha:
                missing_or_corrupt.append(
                    f"Parsed task result {rel} differs from the indexed artifact bytes"
                )
                continue

        indexed_paths.add(rel)
        indexed_hashes[rel] = actual_sha
        valid_count += 1

    runs_dir = suite_root / "runs"
    if parsed_result_hashes is not None:
        for rel in parsed_result_hashes:
            if rel not in declared_paths:
                missing_or_corrupt.append(f"Parsed task result {rel} is missing from artifact-index.json")
    run_subdirs = [d for d in runs_dir.iterdir() if d.is_dir()] if runs_dir.is_dir() else []
    if run_subdirs and len(items) == 0:
        missing_or_corrupt.append("artifact-index.json is empty despite executed runs existing on disk")

    executed_task_ids = executed_suite_task_ids(suite_root, result)

    if plan:
        for task_plan in plan.tasks:
            expected_run_id = run_id_by_task[task_plan.task_id]
            run_d = suite_root / "runs" / expected_run_id
            matched_run_d: Optional[Path] = run_d if run_d.is_dir() else None

            if matched_run_d is None:
                if task_plan.task_id in executed_task_ids:
                    missing_or_corrupt.append(
                        f"Task {task_plan.task_id} was executed but its run directory is missing: runs/{expected_run_id}"
                    )
                continue

            cand_run_id = matched_run_d.name
            ident_data: Dict[str, Any] = {}
            recorded = next((t for t in result.tasks if t.task_id == task_plan.task_id), None) if result else None
            if (
                task_plan.proof_level != ProofLevel.NATIVE
                and recorded is not None
                and recorded.execution_status == ExecutionStatus.PASSED
            ):
                ev_root = matched_run_d / "evidence"
                if not ev_root.is_dir():
                    ev_root = matched_run_d
                execution, evidence, _, _, error = evaluate_task_evidence(
                    task_plan,
                    ev_root,
                    matched_run_d / "junit",
                    recorded.execution_status,
                    recorded.exit_code,
                    run_id=cand_run_id,
                    artifact_reader=read_indexed_evidence if artifact_reader is not None else None,
                )
                if execution != ExecutionStatus.PASSED or evidence != EvidenceStatus.COMPLETE:
                    missing_or_corrupt.append(f"Task {task_plan.task_id} boundary evidence invalid: {error}")

            # 1. Mandatory identity.json
            ident_file = matched_run_d / "identity.json"
            ident_rel = f"runs/{cand_run_id}/identity.json"
            if not ident_file.is_file():
                missing_or_corrupt.append(f"Run {cand_run_id} missing mandatory identity.json on disk")
            elif ident_rel not in indexed_paths:
                missing_or_corrupt.append(f"Run {cand_run_id} identity.json missing from artifact-index.json")
            else:
                try:
                    identity_bytes = (
                        artifact_reader(ident_file)
                        if artifact_reader is not None else ident_file.read_bytes()
                    )
                    if (artifact_reader is not None
                            and hashlib.sha256(identity_bytes).hexdigest() != indexed_hashes[ident_rel]):
                        raise ValueError("identity.json changed after its indexed hash was verified")
                    loaded_ident = json.loads(identity_bytes.decode("utf-8"))
                    ident_data = loaded_ident if isinstance(loaded_ident, dict) else {}
                    m_sha = ident_data.get("marketplace_commit_sha")
                    g_sha = ident_data.get("gonka_source_sha") or ident_data.get("gonka_commit_sha")
                    if (
                        plan.source_identity
                        and plan.source_identity.marketplace_commit_sha
                        and m_sha != plan.source_identity.marketplace_commit_sha
                    ):
                        missing_or_corrupt.append(
                            f"Run {cand_run_id} identity marketplace SHA mismatch: expected {plan.source_identity.marketplace_commit_sha!r}, got {m_sha!r}"
                        )
                    if (
                        plan.source_identity
                        and plan.source_identity.gonka_commit_sha
                        and g_sha != plan.source_identity.gonka_commit_sha
                    ):
                        missing_or_corrupt.append(
                            f"Run {cand_run_id} identity gonka SHA mismatch: expected {plan.source_identity.gonka_commit_sha!r}, got {g_sha!r}"
                        )
                    # The snapshot's identity must pin the Git trees it was
                    # materialised from; a prepared/overlay field means the run
                    # was produced by the retired model and cannot be graded
                    # as immutable-source evidence.
                    for tree_field in ("gonka_tree_sha", "marketplace_tree_sha"):
                        tree_value = ident_data.get(tree_field)
                        if not _is_full_git_sha(tree_value):
                            missing_or_corrupt.append(
                                f"Run {cand_run_id} identity.json missing {tree_field} of the source snapshot"
                            )
                            continue
                        planned_tree = getattr(plan.source_identity, tree_field, None) if plan.source_identity else None
                        if planned_tree and tree_value != planned_tree:
                            missing_or_corrupt.append(
                                f"Run {cand_run_id} identity {tree_field} mismatch: expected {planned_tree!r}, got {tree_value!r}"
                            )
                    for retired in ("gonka_prepared_sha", "gonka_overlay_manifest_sha256"):
                        if ident_data.get(retired) not in (None, ""):
                            missing_or_corrupt.append(
                                f"Run {cand_run_id} identity.json names {retired}, a field of the retired "
                                "prepared-build model"
                            )
                    ident_run_id = ident_data.get("run_id")
                    if not ident_run_id:
                        missing_or_corrupt.append(f"Run {cand_run_id} identity.json missing run_id")
                    elif str(ident_run_id) != expected_run_id:
                        missing_or_corrupt.append(
                            f"Run {cand_run_id} identity run_id {ident_run_id!r} does not match "
                            f"the plan-derived run id {expected_run_id!r}"
                        )
                    ident_task_id = ident_data.get("task_id")
                    if ident_task_id is not None and str(ident_task_id) != task_plan.task_id:
                        missing_or_corrupt.append(
                            f"Run {cand_run_id} identity task_id {ident_task_id!r} does not match "
                            f"the planned task {task_plan.task_id!r}"
                        )
                except Exception as exc:
                    missing_or_corrupt.append(f"Failed to read identity in {ident_file}: {exc}")

            if (
                task_plan.proof_level == ProofLevel.NATIVE
                and artifact_reader is not None
                and recorded is not None
                and recorded.execution_status == ExecutionStatus.PASSED
            ):
                ev_root = matched_run_d / "evidence"
                if not ev_root.is_dir():
                    ev_root = matched_run_d
                execution, evidence, _, _, error = evaluate_task_evidence(
                    task_plan,
                    ev_root,
                    matched_run_d / "junit",
                    recorded.execution_status,
                    recorded.exit_code,
                    run_id=cand_run_id,
                    source_identity=build_task_source_identity(plan, ident_data),
                    e2e_evidence_expected=e2e_evidence_expected,
                    artifact_reader=read_indexed_evidence if artifact_reader is not None else None,
                )
                if execution != ExecutionStatus.PASSED or evidence != EvidenceStatus.COMPLETE:
                    missing_or_corrupt.append(
                        f"Task {task_plan.task_id} native evidence invalid: {error}"
                    )

            # 2. Mandatory expected artifacts from task_plan or catalog
            cat_task = get_task_by_id(task_plan.task_id)
            expected_arts = task_plan.expected_artifacts or (cat_task.expected_artifacts if cat_task else [])
            for exp_art in expected_arts:
                art_name = Path(exp_art).name
                found_file: Optional[Path] = None
                for cand in [
                    matched_run_d / exp_art,
                    matched_run_d / "evidence" / exp_art,
                    matched_run_d / "evidence" / cand_run_id / exp_art,
                ]:
                    if cand.is_file():
                        found_file = cand
                        break
                if not found_file:
                    # A task that failed before producing its evidence is
                    # incomplete, not proof that another task's files were
                    # corrupted. Indexed deletions and prior PASS claims are
                    # still checked strictly above and below.
                    if (
                        recorded is not None
                        and recorded.execution_status in {
                            ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT,
                            ExecutionStatus.CANCELLED, ExecutionStatus.INTERRUPTED,
                        }
                        and recorded.raw_execution_status != ExecutionStatus.PASSED
                        and recorded.evidence_status != EvidenceStatus.COMPLETE
                    ):
                        continue
                    missing_or_corrupt.append(
                        f"Task {task_plan.task_id} run {cand_run_id} missing mandatory artifact on disk: {exp_art}"
                    )
                    continue

                rel_to_suite = found_file.relative_to(suite_root).as_posix()
                if rel_to_suite not in indexed_paths:
                    missing_or_corrupt.append(
                        f"Task {task_plan.task_id} run {cand_run_id} mandatory artifact missing from artifact-index.json: {rel_to_suite}"
                    )

                # 3. Re-validate structured evidence if live-context.json,
                # with the same strictness as the online verifier.
                if art_name == "live-context.json":
                    context_bytes: Optional[bytes] = None
                    if artifact_reader is not None:
                        try:
                            context_bytes = artifact_reader(found_file)
                            if hashlib.sha256(context_bytes).hexdigest() != indexed_hashes.get(rel_to_suite):
                                missing_or_corrupt.append(
                                    f"Parsed structured evidence {rel_to_suite} differs from the indexed artifact bytes"
                                )
                                continue
                        except Exception as exc:
                            missing_or_corrupt.append(
                                f"Cannot safely read structured evidence {rel_to_suite}: {exc}"
                            )
                            continue
                    expected_cps = list(
                        task_plan.expected_checkpoints or (cat_task.expected_checkpoints if cat_task else [])
                    )
                    selector = task_plan.scenario_selector or (cat_task.scenario_selector if cat_task else None)
                    scopes = list(task_plan.evidence_scopes or (cat_task.evidence_scopes if cat_task else []))
                    ok, _, err = verify_live_context(
                        found_file,
                        expected_checkpoints=expected_cps,
                        expected_run_id=expected_run_id,
                        expected_source_identity=build_task_source_identity(plan, ident_data),
                        scenario_selector=selector,
                        evidence_scopes=scopes,
                        e2e_evidence_expected=e2e_evidence_expected,
                        raw_bytes=context_bytes,
                    )
                    if not ok:
                        missing_or_corrupt.append(
                            f"Task {task_plan.task_id} structured evidence invalid in {rel_to_suite}: {err}"
                        )

    elif run_subdirs:
        for run_d in run_subdirs:
            ident_file = run_d / "identity.json"
            ident_rel = f"runs/{run_d.name}/identity.json"
            if not ident_file.is_file():
                missing_or_corrupt.append(f"Run {run_d.name} missing mandatory identity.json on disk")
            elif ident_rel not in indexed_paths:
                missing_or_corrupt.append(f"Run {run_d.name} identity.json missing from artifact-index.json")

    return valid_count, len(items), missing_or_corrupt


def verify_and_recalculate_suite(
    suite_dir: Path,
    *,
    e2e_evidence_expected: bool = True,
    artifact_index_bytes: Optional[bytes] = None,
    artifact_index_checked: bool = False,
    artifact_hasher: Optional[Callable[[Path], str]] = None,
    artifact_sizer: Optional[Callable[[Path], int]] = None,
    artifact_digester: Optional[Callable[[Path], Tuple[str, int]]] = None,
    artifact_reader: Optional[Callable[[Path], bytes]] = None,
    suite_plan_bytes: Optional[bytes] = None,
    suite_result_bytes: Optional[bytes] = None,
    suite_result_checked: bool = False,
) -> SuiteVerification:
    """Verify suite artifact integrity and recalculate the authoritative suite outcome."""
    if artifact_reader is None:
        artifact_reader = read_checked_file_bytes
    suite_root = Path(suite_dir).resolve()
    parsed_result_hashes: Optional[Dict[str, str]] = {} if artifact_reader is not None else None
    plan, stored_result, result = _load_suite_plan_and_results(
        suite_root,
        suite_plan_bytes=suite_plan_bytes,
        suite_result_bytes=suite_result_bytes,
        suite_result_checked=suite_result_checked,
        artifact_reader=artifact_reader,
        parsed_result_hashes=parsed_result_hashes,
    )
    index_file = suite_root / "artifact-index.json"

    valid_art, total_art, art_errors = verify_suite_artifacts_integrity(
        suite_root,
        plan=plan,
        result=result,
        e2e_evidence_expected=e2e_evidence_expected,
        artifact_index_bytes=artifact_index_bytes,
        artifact_index_checked=artifact_index_checked,
        artifact_hasher=artifact_hasher,
        artifact_sizer=artifact_sizer,
        artifact_digester=artifact_digester,
        artifact_reader=artifact_reader,
        parsed_result_hashes=parsed_result_hashes,
    )

    is_interrupted_suite = any(t.execution_status == ExecutionStatus.NOT_RUN for t in result.tasks)
    index_missing = artifact_index_bytes is None if artifact_index_checked else not index_file.is_file()
    if is_interrupted_suite and index_missing:
        art_errors = [e for e in art_errors if e != "artifact-index.json not found"]

    consistency_errors: List[str] = []
    if plan is not None:
        try:
            if stored_result is not None:
                if stored_result.source_identity != plan.source_identity:
                    consistency_errors.append(
                        "Recorded suite result source identity differs from the selected suite plan"
                    )
                if stored_result.created_at_utc != plan.created_at_utc:
                    consistency_errors.append(
                        "Recorded suite creation time differs from the selected suite plan"
                    )
                if stored_result.overall_status == ExecutionStatus.PASSED:
                    recorded_status, _ = calculate_suite_outcome(stored_result.tasks)
                    if recorded_status != ExecutionStatus.PASSED:
                        consistency_errors.append(
                            "Recorded suite status differs from its task outcomes"
                        )
            if result.suite_id != plan.suite_id:
                consistency_errors.append(
                    f"Suite ID mismatch: result has {result.suite_id!r}, plan has {plan.suite_id!r}"
                )
            plan_tids = [t.task_id for t in plan.tasks]
            res_tids = [t.task_id for t in result.tasks]
            if plan_tids != res_tids:
                consistency_errors.append(
                    f"Task set mismatch: plan has {plan_tids}, result has {res_tids}"
                )
            task_sources = [("Task result", result.tasks)]
            if stored_result is not None:
                task_sources.append(("Recorded task", stored_result.tasks))
            for source_name, tasks in task_sources:
                if [task.task_id for task in tasks] != plan_tids:
                    continue
                for task_plan, task in zip(plan.tasks, tasks):
                    expected_run_id = make_task_run_id(
                        plan.suite_id, task_plan.ordinal, task_plan.task_id,
                    )
                    if task.ordinal != task_plan.ordinal:
                        consistency_errors.append(
                            f"{source_name} {task_plan.task_id} ordinal differs from the suite plan"
                        )
                    if task.proof_level != task_plan.proof_level:
                        consistency_errors.append(
                            f"{source_name} {task_plan.task_id} proof level differs from the suite plan"
                        )
                    if (
                        task.execution_status in COMPLETED_EXECUTION_STATUSES
                        and task.run_id != expected_run_id
                    ):
                        consistency_errors.append(
                            f"{source_name} {task_plan.task_id} run ID differs from the suite plan"
                        )
        except Exception as exc:
            consistency_errors.append(f"Failed to verify plan consistency: {exc}")

    all_integrity_errors = art_errors + consistency_errors
    has_corruption = bool(all_integrity_errors)

    if has_corruption:
        for t in result.tasks:
            if t.raw_execution_status is None:
                t.raw_execution_status = t.execution_status
            if t.evidence_status == EvidenceStatus.COMPLETE:
                t.evidence_status = EvidenceStatus.INVALID
            if t.execution_status == ExecutionStatus.PASSED:
                t.execution_status = ExecutionStatus.FAILED

    calculated_status, _ = calculate_suite_outcome(
        tasks=result.tasks,
        is_cancelled=(result.overall_status == ExecutionStatus.CANCELLED),
        has_corruption_or_index_errors=has_corruption,
        has_export_or_secondary_errors=any(len(t.secondary_errors) > 0 for t in result.tasks),
    )
    result.overall_status = calculated_status

    if all_integrity_errors and result.summary_message:
        result.summary_message += f" | Integrity errors: {len(all_integrity_errors)}"

    return SuiteVerification(
        suite_dir=suite_root,
        plan=plan,
        stored_result=stored_result,
        result=result,
        integrity_errors=tuple(all_integrity_errors),
        valid_artifacts=valid_art,
        total_artifacts=total_art,
    )
