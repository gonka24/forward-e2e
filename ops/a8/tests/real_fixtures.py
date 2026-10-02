"""Evidence fixtures taken from the real structures written by scripts/a8_acceptance.py.

Rules for this module:

* Unit-test fixtures mimic the producer's field names, nesting, and types.
  Values from compact reports are reused where available; derived or absent
  values are synthetic and the fixture is marked test-only.
* Recorded evidence documents are loaded verbatim where tests need their actual
  contents.
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

from ops.a8 import source_snapshot
from ops.a8.evidence_model import EVIDENCE_MODEL_E2E, EVIDENCE_MODEL_IMMUTABLE

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / "docs" / "reviews" / "evidence"

# Recorded runs used as sources of truth for the positive fixtures.
LOCK_EXACT_E_EVIDENCE = "a8-c1-exact-e-20260910.json"
PACKAGE_AB_EVIDENCE = "a8-abc-reviewed-20260910.json"
G3_EVIDENCE = "g3-terminal-release-repeat-002.json"
# Keep the packaging check tied to the same filenames that consumers load.
REQUIRED_RECORDED_EVIDENCE = (
    LOCK_EXACT_E_EVIDENCE,
    PACKAGE_AB_EVIDENCE,
    G3_EVIDENCE,
    "a8-go-boundary-20260910/report.json",
    "a8-go-boundary-20260910/raw/go-test.json",
)

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
    templates = [{"path": "a8-ownership.yml", "sha256": "3" * 64}]

    def tree(entries: List[Dict[str, str]]) -> str:
        digest = hashlib.sha256()
        for item in entries:
            digest.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
        return digest.hexdigest()

    return _json_bytes({
        "schema": "a8.external-harness-inputs/1",
        "harness_dir": "/app/ops/a8/harness/testermint",
        "harness_files": harness_files,
        "harness_tree_sha256": tree(harness_files),
        "network_templates": templates,
        "network_templates_sha256": tree(templates),
        "runner_files": [{"path": "a8_external_harness.py", "sha256": "4" * 64}],
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
            "path": "local-test-net/a8-ownership.yml",
            "template": "ops/a8/harness/network/a8-ownership.yml",
            "sha256": "8" * 64, "template_sha256": "8" * 64, "mode": "0o644",
        }],
        "directories": [{"path": "prod-local", "mode": "0o777"}],
        "compose_files_by_pair": {
            pair: ["local-test-net/a8-ownership.yml", "local-test-net/a8-nats.yml"]
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
    """Load the recorded report and raw Go output without simplifying their shape."""
    directory = EVIDENCE_DIR / "a8-go-boundary-20260910"
    return (
        json.loads((directory / "report.json").read_text(encoding="utf-8")),
        (directory / "raw" / "go-test.json").read_text(encoding="utf-8"),
    )


def load_recorded_evidence(name: str) -> Dict[str, Any]:
    """Loads one recorded evidence document from docs/reviews/evidence."""
    return json.loads((EVIDENCE_DIR / name).read_text(encoding="utf-8"))


def _find_phase(document: Any, phase_name: str) -> Dict[str, Any]:
    """Finds a recorded phase by its 'name' anywhere in a recorded document."""
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
    """Synthetic current E2E live-context wrapping recorded business phases.

    The business phases and checkpoints are loaded verbatim from the historical
    recorded run (docs/reviews/evidence/a8-c1-exact-e-20260910.json). The E2E
    envelope (`evidence_model: EVIDENCE_MODEL_IMMUTABLE`) and source/runtime
    identity are synthetically adapted in-memory to match current runner
    invariants. This helper is a synthetic test fixture, not evidence of a live
    E2E run under the current runner; historical disk archives remain unchanged.
    """
    context = load_recorded_evidence(LOCK_EXACT_E_EVIDENCE)
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
    """The recorded lock_exact_e phase (nested inside its own scenario)."""
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
    """The recorded lock-exact-E context stamped with a runner run ID."""
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
        "created_at_utc": "2026-09-11T12:00:00+00:00",
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
    """Reuse receipt structure without presenting adapted transactions as recorded."""
    receipt = copy.deepcopy(template)
    receipt["height"] = str(height)
    receipt["tx_hash"] = hashlib.sha256(label.encode()).hexdigest().upper()
    return receipt


def real_cw20_fault_rollbacks() -> List[Dict[str, Any]]:
    """Synthetic withdrawal failures adapted from recorded settlement failures."""
    return real_claim_settle_phase()["cw20_fault_rollbacks"]


def real_claim_settle_phase() -> Dict[str, Any]:
    """Adapt recorded economics to settlement followed by independent withdrawals.

    The archive predates pull payments. Keep its roles, amounts, and final
    balances; synthesize the intermediate settled state and transaction order.
    Shapes follow claim_settle and query_usdt_payments at 28830a1. These are
    offline examples, not recorded withdrawal executions.
    """
    phase = _find_phase(load_recorded_evidence(PACKAGE_AB_EVIDENCE), "claim_settle")
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
    """The recorded r1_1_refund_e_plus_5_rejected phase, in its passing form.

    The recorded attempt is the real on-chain rejection at E+5. The recorded run
    stored a semantic error from an older assertion helper; the current harness
    writes status PASS with no semantic error when the rejection is correct.
    """
    document = load_recorded_evidence(PACKAGE_AB_EVIDENCE)
    phase = _find_phase(document, "r1_1_refund_e_plus_5_rejected")
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
        "run_id": "package-a-r1-r2-001",
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
    scripts/a8_acceptance.py; the amounts are the ones recorded in
    docs/reviews/evidence/a8-b3-foreign-native-004.json, where the Deal keeps
    all 12345ua8b3foreign while its GNK balance drops to zero.
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
        "recorded_at_utc": "2026-09-09T21:44:05Z",
        "level": "live_network",
        "fixture": {
            "denom": "ua8b3foreign",
            "amount": 12345,
            "genesis_address": "gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny",
            "genesis_source_balance_before": 12345,
            "genesis_source_balance_after": 0,
        },
        "foreign_native_funding": {
            "tx": {
                "tx_hash": "0FA837FF21D285EECCE81FD841098D7E1C9F95E9DA905E9A1AD0F3785EB856B0",
                "height": "189",
                "code": 0,
                "gas_wanted": "200000",
                "gas_used": "95000",
                "events": [],
            },
            "event": {
                "type": "transfer",
                "sender": "gonka1k4swv40ur28fvu54p8mskjj4lxkgsj07u9f8ny",
                "recipient": "gonka1cnuw3f076wgdyahssdkd0g3nr96ckq8cwa2mh029fn5mgf2fmcmsctg4n4",
                "amount": "12345ua8b3foreign",
            },
            "before": dict(roles_zero),
            "after": dict(foreign_after),
        },
        "release": {
            "caller": "gonka1pf7swl0e7xr8adjupt87cj23uqcau3jzrh5qy4",
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
                "tx_hash": "0014234B0447B6BE2E00C24048E0765B7EE07A5A833EE21441A05006546065E4",
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

    The compact report supplies transaction identities and payout totals, but
    this reconstructed context also contains synthetic balance snapshots.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": "synthetic-test-b3-native-004",
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
# Values are the ones recorded in
# docs/reviews/evidence/a8-claim-expiry-positive-043.json: target epoch 5, the
# E+1 rejection in epoch 6 (heights 158/160/160) and the E+2 commit in epoch 7
# (heights 183/184/184). Field names and nesting follow refund_scenario(),
# verify_unclaimed_scenario() and verify_missing_summary_scenario().
# ---------------------------------------------------------------------------

D1_TARGET_EPOCH = 5
D1_BUDGET_MICRO_USDT = 10000000
D1_HOST = "gonka1xs5pw5jml5frd9h25h65szq9n8aanlpn3ex3cu"
D1_BUYER = "gonka1h68g9c6jz0pkt7zsx2jlnt5f4nem0ghczaw9yq"
D1_FEE_RECIPIENT = "gonka15x7vlrmlnmcvvr3htjr7y4u76qgrkjm8aru7ag"
D1_DEAL = "gonka1ltd0maxmte3xf4zshta9j5djrq9cl692ctsp9u5q0p9wss0f5lmsa2r502"
D1_CW20 = "gonka1hrpna9v7vs3stzyd4z3xf00676kf78zpe2u5ksvljswn2vnjp3ys6w65a0"

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
    epoch carries no "claimed" key at all (see the recorded claim in
    docs/reviews/evidence/a8-abc-reviewed-20260910.json, which does carry
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
    """The recorded E+1 rejection (epoch 6, code 5, wasm).

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
            "tx_hash": "8415561C6831907F4F7CF4C1987C4CD0691395FAB29446AE99A1AB543C4FFB4C",
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
    """Recorded E+2 receipt plus synthetic terminal-retry examples for unit tests."""
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
            "tx_hash": "8D83F6BE205BBB9A6F3817CE5BE40C5E390B0508AFAD136A300144F141B08D3A",
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
            "tx_hash": hashlib.sha256(b"synthetic:d1:terminal-repeat").hexdigest().upper(),
            "height": "185",
            "raw_log": "failed to execute message; message index: 0: deal is not refundable",
        },
        "competing_settlement": {
            "layer": "deliver_tx",
            "code": 5,
            "codespace": "wasm",
            "tx_hash": hashlib.sha256(b"synthetic:d1:competing-settlement").hexdigest().upper(),
            "height": "186",
            "raw_log": "failed to execute message; message index: 0: deal is already terminal",
        },
        "cw20_fault_rollback": None,
    }


def real_claim_expiry_positive_context() -> Dict[str, Any]:
    """Synthetic verifier fixture shaped like a claim-expiry live-context.

    Compact evidence supports the main E+1/E+2 receipts and summaries, not
    this full context or its terminal retry receipts. The marker prevents the
    fixture from being ingested as live run evidence.
    """
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": "synthetic-test-claim-expiry-positive-043",
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
    """The three-phase context quoted in the round 5 review; it must fail."""
    context = real_claim_expiry_positive_context()
    context["scenarios"]["claim-expiry-positive"]["phases"] = copy.deepcopy(TOY_D1_PHASES)
    return context


E1_TARGET_EPOCH = 7
E1_HOST = "gonka1echslgnkxhtqluz2avcnftvay3x3d730qkgc65"
E1_BUYER = E1_HOST  # This run aliases Buyer and Host to one address.
E1_FEE_RECIPIENT = "gonka1rvlg2spn48jjt8n7usy73hn9lj0edpyv0fukyn"
E1_DEAL = "gonka1ltd0maxmte3xf4zshta9j5djrq9cl692ctsp9u5q0p9wss0f5lmsa2r502"


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

    The compact PASS evidence supports the target epoch, account identities,
    and primary refund receipts. Terminal retry receipts are synthetic because
    the compact report does not record them. The marker blocks run ingestion.
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
        "run_id": "synthetic-test-network-unconfirmed-stable-e1-002",
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
                            "tx_hash": "500023D438336A47CE26C6CC426AED8471E7C82B44E12B7EA2A45AE5FA07463A",
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
                            "tx_hash": "6986C8C8D1A28C6C232CC0404B0843AAB9AFFF97E785FF52197C524ADC020C39",
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
                            "tx_hash": hashlib.sha256(b"synthetic:e1:terminal-repeat").hexdigest().upper(),
                            "height": "260",
                            "raw_log": "deal is not refundable",
                        },
                        "competing_settlement": {
                            "layer": "deliver_tx",
                            "code": 5,
                            "codespace": "wasm",
                            "tx_hash": hashlib.sha256(b"synthetic:e1:competing-settlement").hexdigest().upper(),
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
# Numbers come from docs/reviews/evidence/g3-terminal-release-repeat-002.json
# (tx 238CDC..., height 214, epoch 8, code 5, wasm); the snapshot layout follows
# terminal_release_snapshot().
# ---------------------------------------------------------------------------

G3_MARKER = "no additional GNK is currently available for release"


def _g3_snapshot(document: Dict[str, Any]) -> Dict[str, Any]:
    """Map archived snapshots to the current producer without changing amounts.

    The archive omits config and entitlements responses. Reconstruct their
    relevant fields from recorded identities and state; empty vesting responses
    follow the archived terminal preconditions. Each call owns its snapshots.
    """
    recorded = document["terminal_snapshot"]
    state = copy.deepcopy(recorded["state"])
    release_status = copy.deepcopy(recorded["release_status"])
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
        "native_status": copy.deepcopy(recorded["native_status"]),
        "vesting_total": {"total_amount": document["terminal_preconditions"]["vesting_total_amount"]},
        "vesting_schedule": {"vesting_schedule": None},
        "bank": copy.deepcopy(recorded["bank_balances"]),
        "settlement_cw20": copy.deepcopy(recorded["settlement_cw20_balances"]),
        "foreign_cw20": copy.deepcopy(recorded["foreign_cw20_balances"]),
    }


def real_terminal_release_repeat_phase() -> Dict[str, Any]:
    """Recorded terminal rejection wrapped in the current synthetic phase layout."""
    document = load_recorded_evidence(G3_EVIDENCE)
    tx = document["terminal_transaction"]
    tx_hash = tx["tx_hash"]
    return {
        "name": "terminal_release_repeat",
        "recorded_at_utc": "2026-09-10T08:15:00Z",
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
        "run_id": "g3-terminal-release-repeat-002",
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 6, "budget_micro_usdt": 10000000},
        "phases": [{"name": "release"}, real_terminal_release_repeat_phase()],
        "scenarios": {},
    }


# ---------------------------------------------------------------------------
# R2 vested gift: loaded verbatim from the recorded Package A run.
# ---------------------------------------------------------------------------

R2_SCENARIO = "r2-vested-gift"


def _recorded_r2_evidence() -> List[Dict[str, Any]]:
    document = load_recorded_evidence(PACKAGE_AB_EVIDENCE)
    runs = document["runs"]["a8abc-a-20260909b"]["cases"]["R2"]["evidence"]
    return runs


def real_r2_gift_phases() -> List[Dict[str, Any]]:
    """The four recorded r2_gift_checkpoint stages, in producer order."""
    evidence = _recorded_r2_evidence()
    stages = {}
    for item in evidence:
        if isinstance(item, dict) and item.get("name") == "r2_gift_checkpoint":
            stages[item.get("stage")] = item
    return [stages[name] for name in ("pre_gift", "fully_locked", "first_unlocked", "final")]


def real_vesting_addition_phase() -> Dict[str, Any]:
    """The recorded vesting_addition phase of the same run."""
    evidence = _recorded_r2_evidence()
    phase = next(item for item in evidence if item.get("name") == "vesting_addition")
    return phase


def ordered_vesting_addition_phase(
    *, unlock_before: bool, same_block: bool = False, foreign_tranche: bool = False,
) -> Dict[str, Any]:
    """Recorded proposal/schedule shapes with synthetic ordered native events.

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
    """The four recorded release phases of the R2 run, in producer order.

    The first two pay out the original claim settlement; the last two are the
    gift payouts recorded between the first_unlocked and final snapshots.
    """
    evidence = _recorded_r2_evidence()
    return [item for item in evidence if item.get("name") == "release"]


def real_r2_scenario_context() -> Dict[str, Any]:
    """The r2-vested-gift scenario exactly as the producer recorded it.

    The whole phase list is kept, including the release receipts: the gift
    payouts are part of the proof, not context around it.
    """
    ordered = [item for item in _recorded_r2_evidence() if isinstance(item, dict)]
    return {
        "schema_version": "1.0.0",
        "kind": "gonka-marketplace-a8-live-context",
        "run_id": "package-a-r1-r2-001",
        "level": "live_network",
        "source": _immutable_source_section(),
        "terms": {"target_epoch": 99, "budget_micro_usdt": 100000000},
        "phases": [],
        "scenarios": {
            R2_SCENARIO: {
                "name": R2_SCENARIO,
                "terms": {"target_epoch": 6, "budget_micro_usdt": 10000000},
                "accounts": {
                    "host": "gonka1c4wttrr9w8e048x6qcylpur8y6pc9vxtf7wyce",
                    "buyer": "gonka18dm4xs6ht5m94yjr98v05tmsrudqqp9mhaftw7",
                    "fee_recipient": "gonka1dvdmtz3u2prf6hawan5ajextkjspuytjjvj4y0",
                },
                "contracts": {
                    "deal": "gonka1wl59k23zngj34l7d42y9yltask7rjlnxgccawc7ltrknp6n52fpshg68n0",
                    "cw20": "gonka1hrpna9v7vs3stzyd4z3xf00676kf78zpe2u5ksvljswn2vnjp3ys6w65a0",
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
# Funded-claim release receipts (scripts/a8_acceptance.py: release)
# ---------------------------------------------------------------------------

FUNDED_CLAIM_RECORDED_AT = "2026-09-09T22:14:31Z"


def _funded_claim_release_source() -> Dict[str, Any]:
    """A real release_unlocked_gnk receipt to reshape into funded-claim records.

    The first settlement release of the recorded R2 run is used because it is a
    genuine successful release with distinct Host and Buyer, real balances and
    the contract's own event set.
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
        "run_id": "funded-claim-001",
        "level": "live_network",
        "source": _immutable_source_section(),
        "phases": [copy.deepcopy(phase)],
    }


def synthetic_late_completed_donation_phase() -> Dict[str, Any]:
    """Synthetic producer-shaped late donations; never recorded chain proof.

    Shape follows scripts/a8_acceptance.py at b37dd3bd and the live run
    e2e-20260930-133451-b331fd. All addresses, amounts and receipts here are
    invented. Empty Bank maps model Cosmos omitting a zero coin balance.
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
