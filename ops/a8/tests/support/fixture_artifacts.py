"""Producer-shaped evidence for synthetic suite packages.

These are offline examples, not evidence of an executed chain or Wasm probe.
Boundary reports reuse recorded runs; only transport identity is adapted.
Cargo examples follow the contract tests and adapters.py at 10662bc.
Expected cases are independent of the verifier.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import hashlib
import json
from xml.etree import ElementTree as ET

from ops.a8.tests.real_fixtures import EVIDENCE_DIR, load_recorded_evidence


def _json_bytes(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def _cargo_suite_log(tests, *, filtered_out):
    """Model serial --nocapture output, including stdout on the start line."""
    lines = [f"running {len(tests)} tests\n"]
    for name, markers in sorted(tests.items()):
        lines.append(f"test {name} ... ")
        lines.extend(f"C_CASE:{marker}\n" for marker in markers)
        lines.append("ok\n")
    lines.append(
        f"test result: ok. {len(tests)} passed; 0 failed; 0 ignored; "
        f"0 measured; {filtered_out} filtered out\n\n"
    )
    return "".join(lines)


def task_artifacts(task, gonka_sha):
    """Return ancillary fixtures and require their paths to match the catalog."""
    artifacts = {}
    if task.task_id == "wasm-abi-boundary":
        # Valid empty Wasm module for package transport only; never executed.
        wasm = b"\x00asm\x01\x00\x00\x00"
        report = load_recorded_evidence("a8-wasm-abi-probe-rerun-20260910.json")
        # Keep recorded observations intact; bind packaging to the placeholder.
        report["wasm_sha256"] = hashlib.sha256(wasm).hexdigest()
        artifacts = {
            "a8_query_boundary.wasm": wasm,
            "abi.json": _json_bytes(report),
        }
    elif task.task_id == "go-boundary":
        recorded = EVIDENCE_DIR / "a8-go-boundary-20260910"
        report = load_recorded_evidence("a8-go-boundary-20260910/report.json")
        # The package belongs to this synthetic run; raw events remain verbatim.
        report["gonka_sha"] = gonka_sha
        artifacts = {
            relative: (recorded / relative).read_bytes()
            for relative in ("build.log", "raw/go-test.json", "raw/exit-code")
        }
        artifacts["report.json"] = _json_bytes(report)
    elif task.task_id in ("ct-network-unconfirmed", "ct-claim-expiry"):
        name = {
            "ct-network-unconfirmed": "network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
            "ct-claim-expiry": "claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
        }[task.task_id]
        artifacts[task.task_id + ".log"] = (
            f"running 1 test\ntest contract::tests::{name} ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 67 filtered out\n"
        ).encode()
    elif task.task_id == "ct-package-c-policy":
        kinds = (
            "handler_error", "malformed_protobuf", "oversized_response",
            "missing_nested_summary", "wrong_host", "wrong_epoch",
            "invalid_participant_address", "unsupported_request",
        )
        # Markers belong to the test that prints them, before its completion.
        # Sources at 10662bc: marketplace-deal/src/contract/tests.rs and
        # marketplace-factory/tests/integration.rs. No recorded Cargo log exists.
        deal_tests = {
            f"contract::tests::c_policy_{kind}": [
                f"r4-{kind}-{suffix}" for suffix in
                ("probe", "e2", "e3", "terminal-refund", "terminal-settle_claim")
            ]
            for kind in kinds
        }
        deal_tests.update({
            f"contract::tests::c_routing_{kind}_{operation}": [
                f"r3-{kind}-{operation}", f"r3-{kind}-{operation}-healthy",
            ]
            for kind in ("handler_error", "malformed_protobuf", "duplicate_routing", "epoch")
            for operation in ("lock", "refund")
        })
        factory_tests = {
            "c_recovery_before_deadline_preserves_ledgers_and_settles_once": [
                f"r5-recover-{suffix}" for suffix in
                ("native", "probe", "e2", "ledger", "refund", "settle", "repeat")
            ],
            "c_network_unconfirmed_refund_rolls_back_retries_and_keeps_terminal_host_only_economics": [
                "r5-cancel-native", "r5-cancel-probe", "r5-cancel-e2", "r6.3",
                "r5-cancel-e3", "r5-cancel-ledger", "r5-cancel-terminal-refund",
                "r5-cancel-terminal-settle_claim",
            ],
        }
        # Cargo runs separate binaries, including the filtered-out factory unit
        # tests. Counts below are frozen to the historical producer above.
        artifacts[task.task_id + ".log"] = (
            _cargo_suite_log(deal_tests, filtered_out=52)
            + _cargo_suite_log({}, filtered_out=9)
            + _cargo_suite_log(factory_tests, filtered_out=40)
        ).encode()
    elif task.exact_test_method:
        suite = ET.Element("testsuite", name="MarketplaceContractAcceptanceTests",
                           tests="1", failures="0", errors="0", skipped="0")
        ET.SubElement(suite, "testcase", classname="MarketplaceContractAcceptanceTests",
                      name=task.exact_test_method + "()", time="1.0")
        artifacts = {
            "junit/TEST-MarketplaceContractAcceptanceTests.xml": ET.tostring(suite),
            "testermint.log": b"Synthetic Testermint fixture: 1 test completed\n",
        }
    required = set(task.expected_artifacts) - {"live-context.json"}
    if required != set(artifacts):
        raise ValueError(f"No explicit artifact fixture for {task.task_id}: {sorted(required ^ set(artifacts))}")
    return artifacts
