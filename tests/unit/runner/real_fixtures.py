"""Producer-shaped synthetic evidence fixtures for offline runner and harness tests.

Rules for this module:

* Unit-test fixtures mimic the producer's field names, nesting, and types
  (`scripts/acceptance_harness.py`, `scripts/run_go_boundary.py`, and
  `scripts/test_wasm_query_boundary.mjs`). The ``real_*`` prefix of the
  helpers below means "the producer's real shape", never "a recorded run".
* Self-contained synthetic fixture documents in `tests/fixtures/evidence/` carry
  `"test_fixture_only": true` on disk so they can never be ingested directly as
  live chain evidence; helpers strip that guard in-memory only when testing the
  verifier's inner rules. Every chain identity in them (and in the in-memory
  fixtures here) is a derivation defined in
  `tests/unit/runner/support/synthetic_evidence.py`, which
  `tests/unit/runner/test_synthetic_evidence.py` re-derives from the committed
  bytes; the documents' own `synthetic_fixture` block names producer, schema
  and derivation rules.
* Negative fixtures are produced by deleting or changing exactly ONE mandatory
  fact of a positive fixture, never by inventing a simplified shape.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from forward_e2e.suite import source_snapshot
from forward_e2e.suite.evidence_model import EVIDENCE_MODEL_E2E, EVIDENCE_MODEL_IMMUTABLE
from tests.unit.runner.support.synthetic_evidence import (
    CLOCK_ORIGIN,
    synthetic_address,
    synthetic_hex,
    synthetic_run_id,
    synthetic_timestamp,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / "tests" / "fixtures" / "evidence"

# Self-contained synthetic fixtures under tests/fixtures/evidence/.
LOCK_EXACT_E_EVIDENCE = "lock-exact-epoch-legacy-context.json"
CLAIM_SETTLEMENT_FAULT_EVIDENCE = "claim-settlement-fault-phase.json"
SETTLED_DEAL_LATE_REFUND_EVIDENCE = "settled-deal-late-refund-and-vested-gift.json"
PACKAGE_AB_EVIDENCE = SETTLED_DEAL_LATE_REFUND_EVIDENCE
G3_EVIDENCE = "terminal-release-repeat.json"
WASM_ABI_EVIDENCE = "wasm-query-allowlist-abi.json"
GO_BOUNDARY_EVIDENCE_DIR = "go-query-error-classification"
# Keep the packaging check tied to the same filenames that consumers load.
REQUIRED_SYNTHETIC_EVIDENCE = (
    LOCK_EXACT_E_EVIDENCE,
    CLAIM_SETTLEMENT_FAULT_EVIDENCE,
    SETTLED_DEAL_LATE_REFUND_EVIDENCE,
    G3_EVIDENCE,
    WASM_ABI_EVIDENCE,
    f"{GO_BOUNDARY_EVIDENCE_DIR}/report.json",
    f"{GO_BOUNDARY_EVIDENCE_DIR}/raw/go-test.json",
    f"{GO_BOUNDARY_EVIDENCE_DIR}/build.log",
    f"{GO_BOUNDARY_EVIDENCE_DIR}/raw/exit-code",
)
REQUIRED_RECORDED_EVIDENCE = REQUIRED_SYNTHETIC_EVIDENCE

# Derivation labels ("documents" in synthetic_evidence.py terms). The first
# three are the labels of the committed documents, so an address derived here
# for a role is the very address the file carries; the rest name the in-memory
# contexts built below, which have no file of their own.
SETTLED_DEAL_DOCUMENT = "settled-deal-late-refund-and-vested-gift"
CLAIM_SETTLEMENT_DOCUMENT = "claim-settlement-fault-phase"
TERMINAL_RELEASE_DOCUMENT = "terminal-release-repeat"
CLAIM_EXPIRY_DOCUMENT = "claim-expiry"
NETWORK_UNCONFIRMED_DOCUMENT = "network-unconfirmed"
FOREIGN_NATIVE_DOCUMENT = "foreign-native-preservation"
FUNDED_CLAIM_DOCUMENT = "funded-claim"
REFUND_BOUNDARY_DOCUMENT = "refund-boundary-and-vesting-addition"

#: Genesis account funded with the foreign native denomination by
#: ``harness/network/foreign-native-genesis.yml`` (``B3_FOREIGN_ADDRESS`` in
#: ``scripts/external_harness.py`` and the Kotlin tests). A fixture key of the
#: product under test, not the identity of any run, so it may appear verbatim.
B3_GENESIS_ADDRESS = "gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny"

#: ``(document, label)`` of every transaction hash this module derives for an
#: in-memory receipt. ``tests/unit/runner/test_synthetic_evidence.py`` rebuilds
#: the admissible hash set from it, so a 64-hex literal copied from a run into
#: one of the factories below is reported as an unlisted hash.
IN_MEMORY_TX_LABELS: set = set()


def fixture_tx_hash(document: str, label: str) -> str:
    """Upper-case 64-hex hash for one named in-memory receipt, derived and registered."""
    IN_MEMORY_TX_LABELS.add((document, label))
    return synthetic_hex(f"{document}:tx:{label}", upper=True)


MARKETPLACE_SHA = "f4518654e967b25d1479660115c13f67bace18df"
GONKA_SOURCE_SHA = "29a58fcf64b87967cb874b6169ce2c61e1f269b1"
GONKA_PREPARED_SHA = "c5afa458de7eb8affa7020f1d0fcd4635ba1fd42"
# These are the tree objects for GONKA_SOURCE_SHA and MARKETPLACE_SHA, respectively.
# Gonka was verified through the GitHub commit API; Marketplace through the local
# Git object database. Do not replace them with convenient syntactically valid SHAs.
GONKA_TREE_SHA = "1fde98b66dfcc891ee6d5c75f5263d60428cbd81"
MARKETPLACE_TREE_SHA = "7fc429b5c0db41373fedb4eb5a25d69ebad5f910"
OVERLAY_MANIFEST_SHA = "b" * 64


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _snapshot_fingerprint(head: str, tree: str, tracked_files: int = 3) -> Dict[str, Any]:
    digest = hashlib.sha256(f"{head}\0{tree}\0{tracked_files}".encode("utf-8")).hexdigest()
    return {
        "schema": source_snapshot.SNAPSHOT_SCHEMA,
        "head": head,
        "tree": tree,
        "index_matches_head": True,
        "tracked_files": tracked_files,
        "tracked_digest": digest,
        "missing_tracked": [],
        "status": [],
        "submodules": [],
        "forbidden": [],
    }


def _source_immutability_bytes(
    *,
    gonka_sha: str = GONKA_SOURCE_SHA,
    marketplace_sha: str = MARKETPLACE_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
) -> bytes:
    before = {
        "gonka": _snapshot_fingerprint(gonka_sha, gonka_tree_sha),
        "marketplace": _snapshot_fingerprint(marketplace_sha, marketplace_tree_sha),
    }
    expected = {"gonka": gonka_sha, "marketplace": marketplace_sha}
    roots = {
        label: source_snapshot.immutability_record(
            label=label, expected_sha=expected[label], before=before[label], after=before[label]
        )
        for label in before
    }
    network = json.loads(_network_manifest_bytes().decode("utf-8"))["post_run_verification"]
    return _json_bytes({
        "schema": "a8.source-immutability-set/2",
        "roots": roots,
        "network_root": network,
        "verdict": "UNCHANGED",
    })


def _harness_inputs_bytes() -> bytes:
    harness_files = [
        {"path": "build.gradle.kts", "sha256": "1" * 64},
        {"path": "src/test/kotlin/MarketplaceContractAcceptanceTests.kt", "sha256": "2" * 64},
    ]
    templates = [{"path": "ownership.yml", "sha256": "3" * 64}]

    def tree(entries: List[Dict[str, str]]) -> str:
        digest = hashlib.sha256()
        for item in entries:
            digest.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
        return digest.hexdigest()

    return _json_bytes({
        "schema": "a8.external-harness-inputs/1",
        "harness_dir": "/app/harness/testermint",
        "harness_files": harness_files,
        "harness_tree_sha256": tree(harness_files),
        "network_templates": templates,
        "network_templates_sha256": tree(templates),
        "runner_files": [{"path": "external_harness.py", "sha256": "4" * 64}],
        "upstream_testermint_test": {
            "path": "testermint/src/test/kotlin/TestermintTest.kt", "sha256": "5" * 64,
        },
        "classpath_file": {
            "path": "/workspace/a8-work/run/testermint-classpath.txt",
            "sha256": "6" * 64, "entries": 42,
        },
    })


def _network_manifest_bytes() -> bytes:
    verification = {
        "verdict": "UNCHANGED",
        "checked_upstream_files": 1,
        "checked_generated_files": 1,
        "violations": [],
        "runtime_additions": [],
    }
    return _json_bytes({
        "schema": "a8.network-manifest/1",
        "gonka_dir": "/workspace/src/gonka",
        "network_root": "/workspace/a8-work/run/network-root",
        "copy_mode": "full_working_tree",
        "b3": False,
        "upstream_copies": [{
            "path": "local-test-net/docker-compose.yml",
            "sha256": "7" * 64, "source_sha256": "7" * 64, "mode": "0o644",
        }],
        "generated": [{
            "path": "local-test-net/ownership.yml",
            "template": "harness/network/ownership.yml",
            "sha256": "8" * 64, "template_sha256": "8" * 64, "mode": "0o644",
        }],
        "directories": [{"path": "prod-local", "mode": "0o777"}],
        "compose_files_by_pair": {
            pair: ["local-test-net/ownership.yml", "local-test-net/nats.yml"]
            for pair in ("genesis", "join1", "join2")
        },
        "ownership_label": "io.gonka.a8.run-id",
        "genesis_provisioner": None,
        "post_run_verification": verification,
    })


_HARNESS_INPUTS_BYTES = _harness_inputs_bytes()
_NETWORK_MANIFEST_BYTES = _network_manifest_bytes()
EXTERNAL_HARNESS_INPUTS_SHA256 = hashlib.sha256(_HARNESS_INPUTS_BYTES).hexdigest()
NETWORK_MANIFEST_SHA256 = hashlib.sha256(_NETWORK_MANIFEST_BYTES).hexdigest()


def write_immutable_companion_files(
    evidence_dir: Path,
    *,
    gonka_sha: str = GONKA_SOURCE_SHA,
    marketplace_sha: str = MARKETPLACE_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
) -> Dict[str, bytes]:
    """Write source-immutability.json, harness-inputs.json, and network-manifest.json."""
    immutability = _source_immutability_bytes(
        gonka_sha=gonka_sha,
        marketplace_sha=marketplace_sha,
        gonka_tree_sha=gonka_tree_sha,
        marketplace_tree_sha=marketplace_tree_sha,
    )
    files = {
        "source-immutability.json": immutability,
        "external-harness/harness-inputs.json": _HARNESS_INPUTS_BYTES,
        "network/network-manifest.json": _NETWORK_MANIFEST_BYTES,
    }
    for relpath, content in files.items():
        path = evidence_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def _immutable_source_section(
    *,
    gonka_sha: str = GONKA_SOURCE_SHA,
    marketplace_sha: str = MARKETPLACE_SHA,
    gonka_tree_sha: str = GONKA_TREE_SHA,
    marketplace_tree_sha: str = MARKETPLACE_TREE_SHA,
) -> Dict[str, Any]:
    immutability = _source_immutability_bytes(
        gonka_sha=gonka_sha,
        marketplace_sha=marketplace_sha,
        gonka_tree_sha=gonka_tree_sha,
        marketplace_tree_sha=marketplace_tree_sha,
    )
    return {
        "evidence_model": EVIDENCE_MODEL_IMMUTABLE,
        "marketplace_commit_sha": marketplace_sha,
        "marketplace_tree_sha": marketplace_tree_sha,
        "gonka_source_sha": gonka_sha,
        "gonka_sha": gonka_sha,
        "gonka_tree_sha": gonka_tree_sha,
        "source_immutability_verdict": "UNCHANGED",
        "source_immutability_sha256": hashlib.sha256(immutability).hexdigest(),
        "external_harness_inputs_sha256": EXTERNAL_HARNESS_INPUTS_SHA256,
        "network_manifest_sha256": NETWORK_MANIFEST_SHA256,
        "runtime": {"gonka_source_sha": gonka_sha},
    }


def go_boundary_evidence() -> tuple[Dict[str, Any], str]:
    """Load the synthetic Go boundary report and raw Go output without simplifying their shape."""
    directory = EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR
    return (
        json.loads((directory / "report.json").read_text(encoding="utf-8")),
        (directory / "raw" / "go-test.json").read_text(encoding="utf-8"),
    )


def load_recorded_evidence(name: str) -> Dict[str, Any]:
    """Loads one synthetic evidence fixture document from tests/fixtures/evidence."""
    return json.loads((EVIDENCE_DIR / name).read_text(encoding="utf-8"))


load_synthetic_evidence = load_recorded_evidence


def _find_phase(document: Any, phase_name: str) -> Dict[str, Any]:
    """Finds a phase by its 'name' anywhere in a fixture document."""
    if isinstance(document, dict):
        if document.get("name") == phase_name:
            return document
        for value in document.values():
            found = _find_phase(value, phase_name)
            if found:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _find_phase(value, phase_name)
            if found:
                return found
    return {}


def real_lock_exact_e_context() -> Dict[str, Any]:
    """Synthetic current E2E live-context wrapping producer-shaped business phases.

    The business phases and checkpoints are loaded from
    `tests/fixtures/evidence/lock-exact-epoch-legacy-context.json`. The E2E
    envelope (`evidence_model: EVIDENCE_MODEL_IMMUTABLE`) and source/runtime
    identity are synthetically adapted in-memory to match current runner
    invariants, and the on-disk `test_fixture_only` guard is stripped in-memory
    so unit tests can exercise `verify_live_context`.
    """
    context = load_recorded_evidence(LOCK_EXACT_E_EVIDENCE)
    context.pop("test_fixture_only", None)
    context.pop("synthetic_fixture", None)
    source = context["source"]
    for stale in (
        "gonka_prepared_sha",
        "gonka_overlay_manifest_sha256",
        "gonka_test_harness_sha",
        "gonka_base_sha",
    ):
        source.pop(stale, None)
    immutable_source = _immutable_source_section()
    runtime = dict(source.get("runtime") or {})
    runtime["gonka_source_sha"] = GONKA_SOURCE_SHA
    source.update(immutable_source)
    source["runtime"] = runtime
    return context


def real_lock_exact_e_phase() -> Dict[str, Any]:
    """The producer-shaped lock_exact_e phase (nested inside its own scenario)."""
    context = real_lock_exact_e_context()
    return context["scenarios"]["lock-exact-e"]["phases"][0]


# Checkpoints the catalog requires for the lock-exact-e task.
LOCK_EXACT_E_CHECKPOINTS = [
    "phase:lock_exact_e",
    "state_transition:Funded->Locked",
    "recipient_locked=true",
    "no_deal_bank_transfer",
    "lock_tx_included_in_epoch_e",
    "target_epoch_e_reached",
]


def real_lock_exact_e_context_for_run(run_id: str) -> Dict[str, Any]:
    """The synthetic lock-exact-E context stamped with a runner run ID."""
    context = real_lock_exact_e_context()
    context["run_id"] = run_id
    return context


def runtime_identity_document(run_id: str) -> Dict[str, Any]:
    """identity.json as written by prepare_runtime_snapshot for that run.

    The current producer defaults to EVIDENCE_MODEL_IMMUTABLE. This synthetic
    identity uses the same evidence model and immutable-source fields.
    """
    return {
        "run_id": run_id,
        # prepare_runtime_snapshot writes datetime.isoformat(): keep its +00:00 form.
        "created_at_utc": CLOCK_ORIGIN.isoformat(),
        "marketplace_source_sha": MARKETPLACE_SHA,
        "marketplace_commit_sha": MARKETPLACE_SHA,
        "marketplace_tree_sha": MARKETPLACE_TREE_SHA,
        "gonka_source_sha": GONKA_SOURCE_SHA,
        "gonka_commit_sha": GONKA_SOURCE_SHA,
        "gonka_checkout_sha": GONKA_SOURCE_SHA,
        "gonka_tree_sha": GONKA_TREE_SHA,
        "evidence_model": EVIDENCE_MODEL_IMMUTABLE,
        "source_policy": "immutable",
        "marketplace_bundle_sha256": "d" * 64,
        "gonka_bundle_sha256": "e" * 64,
        "run_dir": f"/runtime/{run_id}",
    }


def _synthetic_receipt(template: Dict[str, Any], height: int, label: str) -> Dict[str, Any]:
    """Reuse a receipt's structure under a new height and a derived, registered hash."""
    receipt = copy.deepcopy(template)
    receipt["height"] = str(height)
    receipt["tx_hash"] = fixture_tx_hash(CLAIM_SETTLEMENT_DOCUMENT, label)
    return receipt


def real_cw20_fault_rollbacks() -> List[Dict[str, Any]]:
    """Synthetic withdrawal failures adapted from settlement fault fixtures."""
    return real_claim_settle_phase()["cw20_fault_rollbacks"]


def real_claim_settle_phase() -> Dict[str, Any]:
    """Adapt settlement economics to settlement followed by independent withdrawals.

    Shapes follow claim_settle and query_usdt_payments in
    scripts/acceptance_harness.py. These are offline synthetic examples, not
    live withdrawal executions.
    """
    phase = copy.deepcopy(load_recorded_evidence(CLAIM_SETTLEMENT_FAULT_EVIDENCE)["phase"])
    faults = phase["cw20_fault_rollbacks"]
    state = phase["after"]["deal_state"]
    payments = {}
    for entry in faults:
        target = entry["fault"]
        role = "fee" if target["role"] == "fee_recipient" else target["role"]
        payments[role] = {
            "recipient": target["recipient"],
            "accrued_micro_usdt": str(target["amount"]),
            "paid_micro_usdt": "0",
            "pending_micro_usdt": str(target["amount"]),
        }
    phase["settlement_payments"] = payments
    # A successful settlement now records debts without sending any CW20.
    original_tx = phase["settle_tx"]
    height = int(original_tx["height"])
    phase["settle_tx"] = _synthetic_receipt(original_tx, height, "fixture:settle")
    phase["settle_tx"]["events"] = [
        event for event in original_tx["events"]
        if event["type"] == "wasm-claim_settled" or (
            event["type"] == "execute" and any(
                a["key"] == "_contract_address" and a["value"] == phase["deal_config"]["deal_address"]
                for a in event.get("attributes", [])
            )
        )
    ]
    for index, entry in enumerate(faults):
        # All attempts fail; successful withdrawals happen after the fault loop.
        snapshot = entry["before"]
        snapshot["state"] = copy.deepcopy(state)
        entry["after"] = copy.deepcopy(snapshot)
        entry["pending_before"] = copy.deepcopy(payments)
        entry["pending_after"] = copy.deepcopy(payments)
        for key in ("setup_tx", "attempt", "clear_tx"):
            height += 1
            entry[key] = _synthetic_receipt(entry[key], height, f"fixture:fault:{index}:{key}")
        entry["assertion"] = (
            "recipient-selected withdrawal failed and preserved all balances, "
            "settled state, and pending obligations"
        )
    phase["withdrawal_txs"] = {}
    # Each withdrawal executes Deal, emits its payout event, then executes the
    # CW20 transfer. Keep one execution event per contract, not all three
    # historical CW20 executions from the combined settlement transaction.
    executions = {
        attribute["value"]: event
        for event in original_tx["events"] if event["type"] == "execute"
        for attribute in event["attributes"] if attribute["key"] == "_contract_address"
    }
    for role, payment in payments.items():
        height += 1
        receipt = _synthetic_receipt(original_tx, height, f"fixture:withdraw:{role}")
        payout, transfer = [
            event for event in original_tx["events"]
            if any(a["key"] in ("recipient", "to") and a["value"] == payment["recipient"]
                   for a in event.get("attributes", []))
        ]
        receipt["events"] = copy.deepcopy([
            executions[phase["deal_config"]["deal_address"]],
            payout,
            executions[phase["deal_config"]["settlement_cw20"]],
            transfer,
        ])
        phase["withdrawal_txs"][role] = receipt
    repeat = phase["settle_repeat"]
    repeat["attempt"] = _synthetic_receipt(repeat["attempt"], height + 1, "fixture:settle-repeat")
    repeat["proof"].update(height=height + 1, tx_hash=repeat["attempt"]["tx_hash"])
    return phase


def real_r1_refund_phase() -> Dict[str, Any]:
    """The producer-shaped r1_1_refund_e_plus_5_rejected phase, in its passing form."""
    document = load_recorded_evidence(SETTLED_DEAL_LATE_REFUND_EVIDENCE)
    phase = copy.deepcopy(document["refund_e_plus_5_phase"])
    phase["status"] = "PASS"
    phase["semantic_error"] = ""
    phase["invariant_errors"] = []
    return phase


def real_r1_scenario_context() -> Dict[str, Any]:
    """A live-context with the R1 phase nested under its own scenario.

    The target epoch lives in the scenario's terms (5), and the rejection is
    bracketed in epoch 10 = target + 5.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(REFUND_BOUNDARY_DOCUMENT),
        "level": "live_network",
        "source": _immutable_source_section(),
        # Top-level terms deliberately differ: they must never be used for a
        # phase that belongs to a scenario.
        "terms": {"target_epoch": 99, "budget_micro_usdt": 100000000},
        "phases": [],
        "scenarios": {
            "r1-refund-e-plus-5": {
                "name": "r1-refund-e-plus-5",
                "terms": {
                    "target_epoch": 5,
                    "budget_micro_usdt": 10000000,
                    "price_micro_usdt_per_gnk": 200,
                    "fee_bps": 150,
                },
                "phases": [real_r1_refund_phase()],
            }
        },
    }


def real_b3_phase() -> Dict[str, Any]:
    """The b3_foreign_native_successful_release phase.

    Field names, nesting and types follow the producer in
    scripts/acceptance_harness.py, where the Deal keeps all 12345ua8b3foreign
    while its GNK balance drops to zero.
    """
    roles_zero = {"host": 0, "buyer": 0, "fee_recipient": 0, "caller": 0, "deal": 0}
    foreign_after = dict(roles_zero, deal=12345)
    settlement_cw20 = {"host": 0, "buyer": 80000000, "fee_recipient": 150000, "caller": 0, "deal": 10000000}
    foreign_cw20 = {"host": 0, "buyer": 1000000, "fee_recipient": 0, "caller": 0, "deal": 0}
    available = 47409835683225
    buyer_delta = 50000000000
    host_delta = 47359835683225

    ngonka_before = {
        "host": 100000000000,
        "buyer": 616676038844026,
        "fee_recipient": 0,
        "caller": 500000000000,
        "deal": available,
    }
    ngonka_after = {
        "host": ngonka_before["host"] + host_delta,
        "buyer": ngonka_before["buyer"] + buyer_delta,
        "fee_recipient": 0,
        "caller": 500000000000,
        "deal": 0,
    }

    return {
        "name": "b3_foreign_native_successful_release",
        "recorded_at_utc": synthetic_timestamp(1),
        "level": "live_network",
        "fixture": {
            "denom": "ua8b3foreign",
            "amount": 12345,
            "genesis_address": B3_GENESIS_ADDRESS,
            "genesis_source_balance_before": 12345,
            "genesis_source_balance_after": 0,
        },
        "foreign_native_funding": {
            "tx": {
                "tx_hash": fixture_tx_hash(FOREIGN_NATIVE_DOCUMENT, "foreign_native_funding"),
                "height": "189",
                "code": 0,
                "gas_wanted": "200000",
                "gas_used": "95000",
                "events": [],
            },
            "event": {
                "type": "transfer",
                "sender": B3_GENESIS_ADDRESS,
                "recipient": synthetic_address(FOREIGN_NATIVE_DOCUMENT, "deal", contract=True),
                "amount": "12345ua8b3foreign",
            },
            "before": dict(roles_zero),
            "after": dict(foreign_after),
        },
        "release": {
            "caller": synthetic_address(FOREIGN_NATIVE_DOCUMENT, "caller"),
            "caller_ngonka_fee_delta": 0,
            "stable_epoch": {
                "epoch": 7,
                "before_height": 190,
                "tx_height": 191,
                "after_height": 192,
                "same_epoch": True,
            },
            "native_status_before": {"liquid_balance_ngonka": str(available)},
            "state_before": {
                "status": "releasing",
                "released_total_ngonka": "0",
                "buyer_released_ngonka": "0",
                "host_released_ngonka": "0",
            },
            "state_after": {
                "status": "releasing",
                "released_total_ngonka": str(available),
                "buyer_released_ngonka": str(buyer_delta),
                "host_released_ngonka": str(host_delta),
            },
            "tx": {
                "tx_hash": fixture_tx_hash(FOREIGN_NATIVE_DOCUMENT, "release"),
                "height": "191",
                "code": 0,
                "gas_wanted": "2000000",
                "gas_used": "310000",
                "events": [],
            },
            "before": {
                "ngonka": dict(ngonka_before),
                "foreign_native": dict(foreign_after),
                "settlement_cw20": dict(settlement_cw20),
                "foreign_cw20": dict(foreign_cw20),
            },
            "after": {
                "ngonka": dict(ngonka_after),
                "foreign_native": dict(foreign_after),
                "settlement_cw20": dict(settlement_cw20),
                "foreign_cw20": dict(foreign_cw20),
            },
            "expected": {
                "buyer_delta": buyer_delta,
                "host_delta": host_delta,
                "released_total": available,
                "buyer_released": buyer_delta,
                "host_released": host_delta,
            },
            "actual": {
                "buyer_delta": buyer_delta,
                "host_delta": host_delta,
                "available_ngonka": available,
                "foreign_native_deal_after": 12345,
            },
        },
    }


def real_b3_context() -> Dict[str, Any]:
    """Synthetic B3 verifier fixture shaped like producer top-level phases.

    Balances, deltas and receipts are invented so that the release arithmetic
    is exactly consistent; the ``test_fixture_only`` marker keeps the context
    out of any live-evidence path.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(FOREIGN_NATIVE_DOCUMENT),
        "test_fixture_only": True,
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 7, "budget_micro_usdt": 10000000},
        "phases": [real_b3_phase()],
        "scenarios": {},
    }


# ---------------------------------------------------------------------------
# D1 / E1 refund fixtures
#
# Target epoch 5, E+1 rejection in epoch 6 (heights 158/160/160) and E+2
# commit in epoch 7 (heights 183/184/184). Field names and nesting follow
# refund_scenario(), verify_unclaimed_scenario() and
# verify_missing_summary_scenario() in scripts/acceptance_harness.py.
# ---------------------------------------------------------------------------

D1_TARGET_EPOCH = 5
D1_BUDGET_MICRO_USDT = 10000000
D1_HOST = synthetic_address(CLAIM_EXPIRY_DOCUMENT, "host")
D1_BUYER = synthetic_address(CLAIM_EXPIRY_DOCUMENT, "buyer")
D1_FEE_RECIPIENT = synthetic_address(CLAIM_EXPIRY_DOCUMENT, "fee_recipient")
D1_DEAL = synthetic_address(CLAIM_EXPIRY_DOCUMENT, "deal", contract=True)
D1_CW20 = synthetic_address(CLAIM_EXPIRY_DOCUMENT, "settlement_cw20", contract=True)

_D1_BANK_NGONKA = {
    "deal": 0,
    "host": 100000000000,
    "buyer": 616676038844026,
    "fee_recipient": 0,
}


def _d1_snapshot(status: str, cw20_deal: int, cw20_buyer: int, **state: Any) -> Dict[str, Any]:
    """scenario_financial_snapshot() as written by the harness."""
    base_state = {
        "status": status,
        "recipient_locked": True,
        "refund_reason": None,
        "buyer": D1_BUYER,
        "host": D1_HOST,
        "buyer_refund_usdt": "0",
        "host_net_usdt": "0",
        "fee_usdt": "0",
        "gnk_release_policy": "host_only",
    }
    base_state.update(state)
    return {
        "state": base_state,
        "cw20": {
            "deal": cw20_deal,
            "buyer": cw20_buyer,
            "host": 0,
            "fee_recipient": 0,
        },
        "bank_ngonka": dict(_D1_BANK_NGONKA),
    }


_OMITTED = object()


def _native_summary_query(total: int, claimed: Any = _OMITTED) -> Dict[str, Any]:
    """The raw show-epoch-performance-summary-by-participant response.

    The node serialises proto3 defaults by omission, which is why an unclaimed
    epoch carries no "claimed" key at all (whereas a claimed epoch carries
    "claimed": true). Tests may override the field to any representation.
    """
    summary: Dict[str, Any] = {
        "epoch_index": D1_TARGET_EPOCH,
        "participant_id": D1_HOST,
        "rewarded_coins": total,
        "earned_coins": 0,
    }
    if claimed is not _OMITTED:
        summary["claimed"] = claimed
    return {"epochPerformanceSummary": summary}


def _recipient_query(epoch: int = D1_TARGET_EPOCH, deal: str = D1_DEAL) -> Dict[str, Any]:
    return {"entries": [{"epoch": epoch, "recipient": deal, "participant": D1_HOST}]}


def real_unclaimed_precondition_phase(
    epoch: int,
    height: int,
    total: int = 94819671366450,
    positive: bool = True,
    claimed: Any = _OMITTED,
) -> Dict[str, Any]:
    """A native_unclaimed_precondition phase as verify_unclaimed_scenario writes it."""
    return {
        "name": "native_unclaimed_precondition",
        "level": "live_network",
        "summary": _native_summary_query(total, claimed),
        "recipient_query": _recipient_query(),
        "target_epoch": D1_TARGET_EPOCH,
        "observation": {"epoch": epoch, "height": height},
        "claimed": False,
        "identity_valid": True,
        "positive_total_required": positive,
        "zero_total_required": not positive,
        "total_ngonka": total,
        "exact_recipient": True,
    }


def real_claim_expiry_rejected_phase() -> Dict[str, Any]:
    """The E+1 rejection (epoch 6, code 5, wasm) in the producer's shape.

    The producer stores the whole tx_attempt dict plus the dict returned by
    assert_expected_refund_failure under actual_reason.
    """
    marker = (
        "claim-expiry window has not opened: current epoch 6, "
        "target epoch 5, first allowed epoch 7"
    )
    snapshot = _d1_snapshot("locked", cw20_deal=10000000, cw20_buyer=90000000)
    return {
        "name": "refund_rejected",
        "expected_reason": "too_early",
        "actual_reason": {
            "reason": "too_early",
            "state_status": "locked",
            "layer": "deliver_tx",
            "matched_contract_error": marker,
        },
        "attempt": {
            "layer": "deliver_tx",
            "code": 5,
            "codespace": "wasm",
            "tx_hash": fixture_tx_hash(CLAIM_EXPIRY_DOCUMENT, "refund_rejected"),
            "height": "160",
            "gas_wanted": "2000000",
            "gas_used": "128413",
            "raw_log": f"failed to execute message; message index: 0: {marker}",
        },
        "before": copy.deepcopy(snapshot),
        "after": copy.deepcopy(snapshot),
        "observed_epoch": 6,
        "epoch_bracket": {
            "epoch": 6,
            "before_height": 158,
            "tx_height": 160,
            "after_height": 160,
            "same_epoch": True,
        },
    }


def real_claim_expiry_committed_phase() -> Dict[str, Any]:
    """The E+2 refund commit plus the terminal-retry receipts the verifier requires."""
    before = _d1_snapshot("locked", cw20_deal=10000000, cw20_buyer=90000000)
    after = _d1_snapshot(
        "refunded",
        cw20_deal=0,
        cw20_buyer=100000000,
        refund_reason="claim_expiry",
        buyer_refund_usdt=str(D1_BUDGET_MICRO_USDT),
    )
    return {
        "name": "refund_committed",
        "reason": "claim_expiry",
        "observed_epoch": 7,
        "epoch_bracket": {
            "epoch": 7,
            "before_height": 183,
            "tx_height": 184,
            "after_height": 184,
            "same_epoch": True,
        },
        "tx": {
            "tx_hash": fixture_tx_hash(CLAIM_EXPIRY_DOCUMENT, "refund_committed"),
            "height": "184",
            "code": 0,
            "gas_wanted": "2000000",
            "gas_used": "204118",
            "events": [],
        },
        "before": before,
        "after": after,
        "expected": {
            "status": "refunded",
            "buyer_refund": D1_BUDGET_MICRO_USDT,
            "host_usdt": 0,
            "fee_usdt": 0,
            "native_transfers": 0,
        },
        "terminal_repeat": {
            "layer": "deliver_tx",
            "code": 5,
            "codespace": "wasm",
            "tx_hash": fixture_tx_hash(CLAIM_EXPIRY_DOCUMENT, "terminal_repeat"),
            "height": "185",
            "raw_log": "failed to execute message; message index: 0: deal is not refundable",
        },
        "competing_settlement": {
            "layer": "deliver_tx",
            "code": 5,
            "codespace": "wasm",
            "tx_hash": fixture_tx_hash(CLAIM_EXPIRY_DOCUMENT, "competing_settlement"),
            "height": "186",
            "raw_log": "failed to execute message; message index: 0: deal is already terminal",
        },
        "cw20_fault_rollback": None,
    }


def real_claim_expiry_positive_context() -> Dict[str, Any]:
    """Synthetic verifier fixture shaped like a claim-expiry live-context.

    Every receipt, summary and balance is invented to be consistent with the
    E+1/E+2 epoch brackets above. The marker prevents the fixture from being
    ingested as live run evidence.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id("claim-expiry-positive"),
        "test_fixture_only": True,
        "level": "live_network",
        "source": _immutable_source_section(),
        # Top-level terms belong to the bootstrap Deal, never to this scenario.
        "terms": {"target_epoch": 99, "budget_micro_usdt": 100000000},
        "phases": [],
        "scenarios": {
            "claim-expiry-positive": {
                "name": "claim-expiry-positive",
                "terms": {
                    "target_epoch": D1_TARGET_EPOCH,
                    "budget_micro_usdt": D1_BUDGET_MICRO_USDT,
                    "price_micro_usdt_per_gnk": 1000000,
                    "fee_bps": 150,
                },
                "accounts": {
                    "host": D1_HOST,
                    "buyer": D1_BUYER,
                    "fee_recipient": D1_FEE_RECIPIENT,
                },
                "contracts": {"deal": D1_DEAL, "cw20": D1_CW20},
                "phases": [
                    real_unclaimed_precondition_phase(epoch=6, height=158),
                    real_claim_expiry_rejected_phase(),
                    real_unclaimed_precondition_phase(epoch=7, height=183),
                    real_claim_expiry_committed_phase(),
                ],
            }
        },
    }


TOY_D1_PHASES: List[Dict[str, Any]] = [
    {
        "name": "native_unclaimed_precondition",
        "claimed": False,
        "positive_total_required": True,
        "total_ngonka": 1,
    },
    {"name": "refund_rejected", "expected_reason": "too_early"},
    {
        "name": "refund_committed",
        "reason": "claim_expiry",
        "tx": {"code": 0},
        "after": {"state": {"status": "refunded"}},
    },
]


def toy_claim_expiry_context() -> Dict[str, Any]:
    """A context whose three phases carry only the self-declared flags; it must fail.

    It is the shape a verifier that trusted ``claimed``/``expected_reason``
    without the native summary, receipt and epoch bracket would accept.
    """
    context = real_claim_expiry_positive_context()
    context["scenarios"]["claim-expiry-positive"]["phases"] = copy.deepcopy(TOY_D1_PHASES)
    return context


E1_TARGET_EPOCH = 7
E1_HOST = synthetic_address(NETWORK_UNCONFIRMED_DOCUMENT, "host")
E1_BUYER = E1_HOST  # This fixture aliases Buyer and Host to one address.
E1_FEE_RECIPIENT = synthetic_address(NETWORK_UNCONFIRMED_DOCUMENT, "fee_recipient")
E1_DEAL = synthetic_address(NETWORK_UNCONFIRMED_DOCUMENT, "deal", contract=True)


def real_summary_absent_phase(offset: int, height: int) -> Dict[str, Any]:
    """native_summary_absent exactly at target_epoch + offset."""
    stderr = (
        "Error: rpc error: code = NotFound desc = not found: "
        "key not found: unknown request"
    )
    return {
        "name": "native_summary_absent",
        "level": "live_network",
        "target_epoch": E1_TARGET_EPOCH,
        "host": E1_HOST,
        "expected_offset": offset,
        "observation": {"epoch": E1_TARGET_EPOCH + offset, "height": height},
        "native_query": {
            "returncode": 1,
            "stdout": "",
            "stderr": stderr,
            "grpc_code": "NotFound",
            "grpc_description": "not found",
            "exact_native_handler_not_found": True,
        },
        "recipient_query": {
            "entries": [
                {"epoch": E1_TARGET_EPOCH, "recipient": E1_DEAL, "participant": E1_HOST}
            ]
        },
        "expected": {
            "summary_absent": True,
            "transport_available": True,
            "exact_recipient": True,
        },
    }


def _e1_snapshot(status: str, cw20_deal: int, cw20_buyer: int, **state: Any) -> Dict[str, Any]:
    base_state = {
        "status": status,
        "recipient_locked": True,
        "refund_reason": None,
        "buyer": E1_BUYER,
        "host": E1_HOST,
        "gnk_release_policy": "host_only",
    }
    base_state.update(state)
    return {
        "state": base_state,
        "cw20": {
            "deal": cw20_deal,
            "buyer": cw20_buyer,
            "host": cw20_buyer if E1_HOST == E1_BUYER else 0,
            "fee_recipient": 0,
        },
        "bank_ngonka": {"deal": 0, "host": 1, "buyer": 2, "fee_recipient": 3},
    }


def real_network_unconfirmed_context() -> Dict[str, Any]:
    """Synthetic verifier fixture shaped like the E1 live-context.

    Both native-summary absences, the E+2 rejection, the E+3 refund and the
    terminal retry receipts are invented to be mutually consistent. The marker
    blocks run ingestion.
    """
    marker = "Gonka query failed for /inference.inference.Query/EpochPerformanceSummaryByParticipant"
    rejected_snapshot = _e1_snapshot("locked", cw20_deal=10000000, cw20_buyer=90000000)
    committed_before = _e1_snapshot("locked", cw20_deal=10000000, cw20_buyer=90000000)
    committed_after = _e1_snapshot(
        "refunded",
        cw20_deal=0,
        cw20_buyer=100000000,
        refund_reason="network_unconfirmed",
    )
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(NETWORK_UNCONFIRMED_DOCUMENT),
        "test_fixture_only": True,
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 99, "budget_micro_usdt": 100000000},
        "phases": [],
        "scenarios": {
            "network-unconfirmed": {
                "name": "network-unconfirmed",
                "terms": {
                    "target_epoch": E1_TARGET_EPOCH,
                    "budget_micro_usdt": 10000000,
                    "price_micro_usdt_per_gnk": 1000000,
                    "fee_bps": 150,
                },
                "accounts": {
                    "host": E1_HOST,
                    "buyer": E1_BUYER,
                    "fee_recipient": E1_FEE_RECIPIENT,
                },
                "contracts": {"deal": E1_DEAL, "cw20": "synthetic-e1-cw20"},
                "phases": [
                    real_summary_absent_phase(offset=2, height=233),
                    {
                        "name": "refund_rejected",
                        "expected_reason": "network_unconfirmed_too_early",
                        "actual_reason": {
                            "reason": "network_unconfirmed_too_early",
                            "state_status": "locked",
                            "layer": "deliver_tx",
                            "matched_contract_error": marker,
                        },
                        "attempt": {
                            "layer": "deliver_tx",
                            "code": 5,
                            "codespace": "wasm",
                            "tx_hash": fixture_tx_hash(NETWORK_UNCONFIRMED_DOCUMENT, "refund_rejected"),
                            "height": "234",
                            "raw_log": f"failed to execute message; message index: 0: {marker}",
                        },
                        "before": copy.deepcopy(rejected_snapshot),
                        "after": copy.deepcopy(rejected_snapshot),
                        "observed_epoch": E1_TARGET_EPOCH + 2,
                        "epoch_bracket": {
                            "epoch": E1_TARGET_EPOCH + 2,
                            "before_height": 233,
                            "tx_height": 234,
                            "after_height": 234,
                            "same_epoch": True,
                        },
                    },
                    real_summary_absent_phase(offset=3, height=258),
                    {
                        "name": "refund_committed",
                        "reason": "network_unconfirmed",
                        "observed_epoch": E1_TARGET_EPOCH + 3,
                        "epoch_bracket": {
                            "epoch": E1_TARGET_EPOCH + 3,
                            "before_height": 258,
                            "tx_height": 259,
                            "after_height": 259,
                            "same_epoch": True,
                        },
                        "tx": {
                            "tx_hash": fixture_tx_hash(NETWORK_UNCONFIRMED_DOCUMENT, "refund_committed"),
                            "height": "259",
                            "code": 0,
                            "gas_wanted": "2000000",
                            "gas_used": "211000",
                            "events": [],
                        },
                        "before": committed_before,
                        "after": committed_after,
                        "expected": {
                            "status": "refunded",
                            "buyer_refund": 10000000,
                            "host_usdt": 0,
                            "fee_usdt": 0,
                            "native_transfers": 0,
                        },
                        "terminal_repeat": {
                            "layer": "deliver_tx",
                            "code": 5,
                            "codespace": "wasm",
                            "tx_hash": fixture_tx_hash(NETWORK_UNCONFIRMED_DOCUMENT, "terminal_repeat"),
                            "height": "260",
                            "raw_log": "deal is not refundable",
                        },
                        "competing_settlement": {
                            "layer": "deliver_tx",
                            "code": 5,
                            "codespace": "wasm",
                            "tx_hash": fixture_tx_hash(NETWORK_UNCONFIRMED_DOCUMENT, "competing_settlement"),
                            "height": "261",
                            "raw_log": "deal is already terminal",
                        },
                        "cw20_fault_rollback": None,
                    },
                ],
            }
        },
    }


# ---------------------------------------------------------------------------
# G3 terminal release repeat
#
# Amounts, heights (213/214/215), epoch 8 and the (code 5, wasm) rejection come
# from tests/fixtures/evidence/terminal-release-repeat.json; the snapshot
# layout follows terminal_release_snapshot().
# ---------------------------------------------------------------------------

G3_MARKER = "no additional GNK is currently available for release"


def _g3_snapshot(document: Dict[str, Any]) -> Dict[str, Any]:
    """Map fixture snapshots to the current producer without changing amounts.

    Reconstruct config and entitlements responses from fixture identities and
    state; empty vesting responses follow the terminal preconditions. Each call
    owns its snapshots.
    """
    snapshot = document["terminal_snapshot"]
    state = copy.deepcopy(snapshot["state"])
    release_status = copy.deepcopy(snapshot["release_status"])
    release_status["gnk_release_policy"] = copy.deepcopy(state["gnk_release_policy"])
    return {
        "state": state,
        "config": {
            "deal_address": document["contracts"]["deal"],
            "host": document["roles"]["host"],
            "fee_recipient": document["roles"]["fee_recipient"],
            "fee_bps": 150,
            "target_epoch": 6,
        },
        "entitlements": {key: copy.deepcopy(state[key]) for key in (
            "work_ngonka", "reward_ngonka", "total_claim_ngonka",
            "buyer_entitlement_ngonka", "host_entitlement_ngonka", "gnk_release_policy",
        )},
        "release_status": release_status,
        "native_status": copy.deepcopy(snapshot["native_status"]),
        "vesting_total": {"total_amount": document["terminal_preconditions"]["vesting_total_amount"]},
        "vesting_schedule": {"vesting_schedule": None},
        "bank": copy.deepcopy(snapshot["bank_balances"]),
        "settlement_cw20": copy.deepcopy(snapshot["settlement_cw20_balances"]),
        "foreign_cw20": copy.deepcopy(snapshot["foreign_cw20_balances"]),
    }


def real_terminal_release_repeat_phase() -> Dict[str, Any]:
    """Producer-shaped terminal rejection wrapped in the current synthetic phase layout."""
    document = load_recorded_evidence(G3_EVIDENCE)
    tx = document["terminal_transaction"]
    tx_hash = tx["tx_hash"]
    return {
        "name": "terminal_release_repeat",
        "recorded_at_utc": synthetic_timestamp(2),
        "level": "live_network",
        "status": "PASS",
        "caller": {"address": document["roles"]["caller"], "role": "independent_fee_payer"},
        "preconditions": {key: document["terminal_preconditions"][key] for key in (
            "positive_total_claim_ngonka", "released_total_ngonka",
            "buyer_original_remaining_ngonka", "host_original_remaining_ngonka",
            "liquid_balance_ngonka", "remaining_vesting_ngonka", "scheduled_vesting_ngonka",
        )},
        "epoch_before": {"epoch": 8, "height": 213},
        "epoch_after": {"epoch": 8, "height": 215},
        "tx": {key: value for key, value in tx.items() if key != "epoch"},
        "terminal_rejection": {
            "contract_error": "NothingToRelease",
            "matched_contract_error": G3_MARKER,
            "layer": "deliver_tx",
            "code": 5,
            "codespace": "wasm",
            "tx_hash": tx_hash,
            "height": 214,
        },
        "semantic_error": "",
        "before": _g3_snapshot(document),
        "after": _g3_snapshot(document),
        "caller_bank_deltas": {"ngonka": 0},
        "invariant_errors": [],
        "expected": {
            "deliver_tx_included": True,
            "contract_error": "NothingToRelease",
            "arbitrary_error_accepted": False,
            "state_entitlements_and_counters_unchanged": True,
            "deal_gnk_transfers": 0,
            "all_tracked_cw20_balances_unchanged": True,
            "repeat_payout": 0,
            "only_caller_gnk_fee_may_change": True,
        },
    }


def real_terminal_release_repeat_context() -> Dict[str, Any]:
    """G3 appends its phase to the top-level phases list."""
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(TERMINAL_RELEASE_DOCUMENT),
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 6, "budget_micro_usdt": 10000000},
        "phases": [{"name": "release"}, real_terminal_release_repeat_phase()],
        "scenarios": {},
    }


# ---------------------------------------------------------------------------
# R2 vested gift: loaded from the synthetic settled-deal fixture.
# ---------------------------------------------------------------------------

R2_SCENARIO = "r2-vested-gift"


def _r2_fixture_phases() -> List[Dict[str, Any]]:
    document = load_recorded_evidence(SETTLED_DEAL_LATE_REFUND_EVIDENCE)
    return copy.deepcopy(document["vested_gift_phases"])


def real_r2_gift_phases() -> List[Dict[str, Any]]:
    """The four r2_gift_checkpoint stages of the fixture, in producer order."""
    evidence = _r2_fixture_phases()
    stages = {}
    for item in evidence:
        if isinstance(item, dict) and item.get("name") == "r2_gift_checkpoint":
            stages[item.get("stage")] = item
    return [stages[name] for name in ("pre_gift", "fully_locked", "first_unlocked", "final")]


def real_vesting_addition_phase() -> Dict[str, Any]:
    """The vesting_addition phase of the same fixture sequence."""
    evidence = _r2_fixture_phases()
    phase = next(item for item in evidence if item.get("name") == "vesting_addition")
    return phase


def ordered_vesting_addition_phase(
    *, unlock_before: bool, same_block: bool = False, foreign_tranche: bool = False,
) -> Dict[str, Any]:
    """The fixture's proposal/schedule shapes with ordered native events.

    Native event fields and aggregation order follow Gonka keeper.go/types/events.go
    at 0becc23a89afae323f07a4bebe9ce289212db738. Only amounts, heights and timing
    are adapted; negative tests must mutate one mandatory fact of this fixture.
    """
    phase = real_vesting_addition_phase()
    recipient = phase["after"]["vesting_schedule"]["participant_address"]
    before = copy.deepcopy(phase["after"])
    before["vesting_schedule"]["epoch_amounts"] = [
        {"coins": [{"amount": str(amount), "denom": "ngonka"}]} for amount in (70, 60)
    ]
    after = copy.deepcopy(before)
    remaining = (66, 5) if unlock_before else (65,)
    after["vesting_schedule"]["epoch_amounts"] = [
        {"coins": [{"amount": str(amount), "denom": "ngonka"}]} for amount in remaining
    ]
    released = 70 if unlock_before else 76
    # A second denomination is paid in the first unlock, so it occurs only in
    # the pre-state and native total, never in the remaining schedule.
    if foreign_tranche:
        before["vesting_schedule"]["epoch_amounts"][0]["coins"].append({"amount": "5", "denom": "uatom"})
    vest = {"type": "transfer_with_vesting", "attributes": [
        {"key": "recipient", "value": recipient, "index": True},
        {"key": "amount", "value": "11ngonka", "index": True},
        {"key": "vesting_epochs", "value": "2", "index": True},
        {"key": "sender", "value": phase["proposal"]["proposal"]["messages"][0]["value"]["sender"], "index": True},
    ]}
    unlock = {"type": "unlock_tokens", "attributes": [
        {"key": "unlocked_amount", "value": f"{released}ngonka" + (",5uatom" if foreign_tranche else ""), "index": True},
        {"key": "participants_unlocked", "value": "1", "index": True},
        {"key": "participants_processed", "value": "1", "index": True},
    ]}
    events = [unlock, vest] if unlock_before else [vest, unlock]
    for i, event in enumerate(events):
        event.update(height=261 if same_block else 261 + i, event_index=i if same_block else 0)
    phase["proposal"]["proposal"]["messages"][0]["value"]["amount"][0]["amount"] = "11"
    funding_transfer = next(event for event in phase["funding_tx"]["events"] if event["type"] == "transfer")
    next(attr for attr in funding_transfer["attributes"] if attr["key"] == "amount")["value"] = "11ngonka"
    phase.update(
        vesting_event_model="ordered/2", recipient=recipient, vesting_events=events,
        amount_ngonka=11, before=before, after=after,
        addition_by_epoch=[{"ngonka": 6}, {"ngonka": 5}],
        expected=[{"ngonka": amount} for amount in remaining],
        before_height=260, after_height=262, before_epoch=10, after_epoch=10,
        before_bank_ngonka=100, after_bank_ngonka=100 + released,
        released_prefix_count=1, released_prefix_ngonka=released,
        eligible_prefix_count=1, unlock_events=[unlock],
        assertion="ordered native vesting events reproduce the remaining schedule and bank payout",
    )
    return phase


def real_r2_release_phases() -> List[Dict[str, Any]]:
    """The four release phases of the R2 fixture sequence, in producer order.

    The first two pay out the original claim settlement; the last two are the
    gift payouts that sit between the first_unlocked and final snapshots.
    """
    evidence = _r2_fixture_phases()
    return [item for item in evidence if item.get("name") == "release"]


def real_r2_scenario_context() -> Dict[str, Any]:
    """The r2-vested-gift scenario in the producer's full phase order.

    The whole phase list is kept, including the release receipts: the gift
    payouts are part of the proof, not context around it. Accounts and
    contracts are the fixture document's own role derivations, i.e. the same
    addresses that occur inside its phases.
    """
    ordered = [item for item in _r2_fixture_phases() if isinstance(item, dict)]
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(REFUND_BOUNDARY_DOCUMENT),
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 99, "budget_micro_usdt": 100000000},
        "phases": [],
        "scenarios": {
            R2_SCENARIO: {
                "name": R2_SCENARIO,
                "terms": {"target_epoch": 6, "budget_micro_usdt": 10000000},
                "accounts": {
                    role: synthetic_address(SETTLED_DEAL_DOCUMENT, role)
                    for role in ("host", "buyer", "fee_recipient")
                },
                "contracts": {
                    "deal": synthetic_address(SETTLED_DEAL_DOCUMENT, "deal", contract=True),
                    "cw20": synthetic_address(SETTLED_DEAL_DOCUMENT, "settlement_cw20", contract=True),
                },
                "phases": ordered,
            }
        },
    }


def real_package_a_context() -> Dict[str, Any]:
    """Package A runs R1 and R2 in one context with two named scenarios."""
    context = real_r2_scenario_context()
    r1 = real_r1_scenario_context()
    context["scenarios"]["r1-refund-e-plus-5"] = r1["scenarios"]["r1-refund-e-plus-5"]
    return context


def real_claim_expiry_zero_context() -> Dict[str, Any]:
    """The zero-reward D1 branch: same boundaries, exact zero unclaimed total."""
    context = real_claim_expiry_positive_context()
    context["run_id"] = "synthetic-test-claim-expiry-zero-044"
    scenario = context["scenarios"].pop("claim-expiry-positive")
    scenario["name"] = "claim-expiry-zero"
    scenario["phases"][0] = real_unclaimed_precondition_phase(
        epoch=6, height=158, total=0, positive=False
    )
    scenario["phases"][2] = real_unclaimed_precondition_phase(
        epoch=7, height=183, total=0, positive=False
    )
    context["scenarios"]["claim-expiry-zero"] = scenario
    return context


def with_native_claimed(context: Dict[str, Any], claimed: Any) -> Dict[str, Any]:
    """Rewrites summary.epochPerformanceSummary.claimed in every D1 precondition.

    Only the native summary is touched: the self-declared phase.claimed flag
    stays false, which is exactly the forgery the verifier must refuse.
    """
    context = copy.deepcopy(context)
    for scenario in context.get("scenarios", {}).values():
        for phase in scenario.get("phases", []) or []:
            if phase.get("name") != "native_unclaimed_precondition":
                continue
            summary = phase["summary"]["epochPerformanceSummary"]
            summary["claimed"] = claimed
    return context


def r2_stage(context: Dict[str, Any], stage: str) -> Dict[str, Any]:
    """The r2_gift_checkpoint phase of one stage inside an R2 context."""
    for phase in context["scenarios"][R2_SCENARIO]["phases"]:
        if phase.get("name") == "r2_gift_checkpoint" and phase.get("stage") == stage:
            return phase
    raise KeyError(stage)


def r2_addition(context: Dict[str, Any]) -> Dict[str, Any]:
    """The vesting_addition phase inside an R2 context."""
    for phase in context["scenarios"][R2_SCENARIO]["phases"]:
        if phase.get("name") == "vesting_addition":
            return phase
    raise KeyError("vesting_addition")


def r2_releases(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The release phases of an R2 context, in producer order.

    Indexes 0 and 1 pay out the original claim settlement; 2 and 3 are the two
    gift payouts recorded between the first_unlocked and final snapshots.
    """
    return [
        phase
        for phase in context["scenarios"][R2_SCENARIO]["phases"]
        if phase.get("name") == "release"
    ]


# ---------------------------------------------------------------------------
# Funded-claim release receipts (scripts/acceptance_harness.py: release)
# ---------------------------------------------------------------------------

FUNDED_CLAIM_RECORDED_AT = synthetic_timestamp(3)


def _funded_claim_release_source() -> Dict[str, Any]:
    """A producer-shaped release_unlocked_gnk receipt to reshape into funded-claim records.

    The first settlement release of the R2 fixture sequence is used because it
    is a successful release with distinct Host and Buyer, consistent balances
    and the contract's own event set.
    """
    return real_r2_release_phases()[0]


def legacy_funded_claim_release_phase() -> Dict[str, Any]:
    """The `release` record as the producer wrote it before the shapes merged.

    No epoch_bracket, caller, caller_native_fee_delta, foreign_cw20_* or
    actual.address_delta / actual.coincident_recipients: exactly the key set of
    the pre-unification a8_acceptance.release.
    """
    source = _funded_claim_release_source()
    state_after = source["state_after"]
    return {
        "name": "release",
        "recorded_at_utc": FUNDED_CLAIM_RECORDED_AT,
        "tx": source["tx"],
        "state_before": source["state_before"],
        "state_after": state_after,
        "bank_before": source["bank_before"],
        "bank_after": source["bank_after"],
        "expected": source["expected"],
        "actual": {
            "buyer_delta": source["actual"]["buyer_delta"],
            "host_delta": source["actual"]["host_delta"],
            "released_total": int(state_after["released_total_ngonka"]),
            "buyer_released": int(state_after["buyer_released_ngonka"]),
            "host_released": int(state_after["host_released_ngonka"]),
        },
    }


def unified_funded_claim_release_phase() -> Dict[str, Any]:
    """The `release` record close_gnk_release writes for both commands today."""
    phase = _funded_claim_release_source()
    state_after = phase["state_after"]
    phase["recorded_at_utc"] = FUNDED_CLAIM_RECORDED_AT
    phase["actual"] = {
        **phase["actual"],
        "released_total": int(state_after["released_total_ngonka"]),
        "buyer_released": int(state_after["buyer_released_ngonka"]),
        "host_released": int(state_after["host_released_ngonka"]),
    }
    return phase


def funded_claim_release_context(phase: Dict[str, Any]) -> Dict[str, Any]:
    """A top-level funded-claim context carrying one release phase.

    The funded-claim task reads the top-level scope only, which is where
    append_phase writes.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": synthetic_run_id(FUNDED_CLAIM_DOCUMENT),
        "level": "live_network",
        "source": _immutable_source_section(),
        "phases": [copy.deepcopy(phase)],
    }


def synthetic_late_completed_donation_phase() -> Dict[str, Any]:
    """Synthetic producer-shaped late donations; never recorded chain proof.

    Shape follows the late_completed_donation_release phase that late_donation
    in scripts/acceptance_harness.py appends.
    All addresses, amounts and receipts here are invented. Empty Bank maps
    model Cosmos omitting a zero coin balance.
    """
    roles = {role: f"synthetic-{role}" for role in
             ("donor", "deal", "host", "buyer", "fee_recipient", "caller")}

    def event(kind, attributes):
        return {"type": kind, "attributes": [
            {"key": key, "value": str(value)} for key, value in attributes.items()
        ]}

    def transfer(sender, recipient, amount):
        return event("transfer", {"sender": sender, "recipient": recipient,
                                  "amount": f"{amount}ngonka"})

    baseline = {
        "state": {"status": "completed", "buyer": roles["buyer"],
                  "total_claim_ngonka": "100", "released_total_ngonka": "100",
                  "buyer_released_ngonka": "50", "host_released_ngonka": "50",
                  "gnk_release_policy": {"proportional": {
                      "buyer_share_numerator": "5", "share_denominator": "10"}}},
        "release_status": {"released_total_ngonka": "100",
                           "buyer_original_remaining_ngonka": "0",
                           "host_original_remaining_ngonka": "0"},
        "native_status": {"liquid_balance_ngonka": "0", "remaining_vesting_ngonka": "0"},
        "vesting_total": {"total_amount": None},
        "vesting_schedule": {"vesting_schedule": None},
        "config": {}, "entitlements": {}, "settlement_cw20": {},
        "foreign_cw20": {"deal": 0},
        "bank": {"deal": {}, "buyer": {"ngonka": 1000}, "host": {"ngonka": 2000},
                 "fee_recipient": {"ngonka": 3000}, "caller": {"ngonka": 4000}},
    }
    releases = []
    for index, (buyer_delta, host_delta) in enumerate(((1, 2), (2, 1))):
        before = copy.deepcopy(baseline if index == 0 else releases[0]["after"])
        funded = copy.deepcopy(before)
        funded["bank"]["deal"] = {"ngonka": 3}
        after = copy.deepcopy(before)
        after["bank"]["buyer"]["ngonka"] += buyer_delta
        after["bank"]["host"]["ngonka"] += host_delta
        for key, delta in (("released_total_ngonka", 3),
                           ("buyer_released_ngonka", buyer_delta),
                           ("host_released_ngonka", host_delta)):
            after["state"][key] = str(int(before["state"][key]) + delta)
        expected = {"released_total": int(after["state"]["released_total_ngonka"]),
                    "buyer_released": int(after["state"]["buyer_released_ngonka"]),
                    "host_released": int(after["state"]["host_released_ngonka"]),
                    "buyer_delta": buyer_delta, "host_delta": host_delta}
        release_attrs = {"entry_point": "release_unlocked_gnk", "_contract_address": roles["deal"],
                         "deal": roles["deal"], "buyer": roles["buyer"], "host": roles["host"],
                         "available_balance_ngonka": 3, "buyer_delta_ngonka": buyer_delta,
                         "host_delta_ngonka": host_delta,
                         **{key: after["state"][key] for key in
                            ("released_total_ngonka", "buyer_released_ngonka", "host_released_ngonka")}}
        releases.append({
            "label": "first" if index == 0 else "second", "amount_ngonka": 3,
            "before": before, "before_release": funded, "after": after,
            "expected": expected, "actual": {"buyer_delta": buyer_delta, "host_delta": host_delta},
            "donation_tx": {"code": 0, "height": 10 + index * 20,
                            "tx_hash": f"synthetic-donation-{index}",
                            "events": [transfer(roles["donor"], roles["deal"], 3)]},
            "release_tx": {"code": 0, "height": 20 + index * 20,
                           "tx_hash": f"synthetic-release-{index}", "events": [
                               event("wasm-gnk_released", release_attrs),
                               transfer(roles["deal"], roles["buyer"], buyer_delta),
                               transfer(roles["deal"], roles["host"], host_delta)]},
        })
    return {"name": "late_completed_donation_release", "roles": roles,
            "rounding": {"buyer_numerator": 5, "denominator": 10,
                         "independent_buyer_per_donation": 1, "remainder": 5},
            "donor_funding_tx": {"code": 0, "height": 1, "tx_hash": "synthetic-funding",
                                 "events": [transfer("synthetic-funder", roles["donor"], 1000006)]},
            "releases": releases}
