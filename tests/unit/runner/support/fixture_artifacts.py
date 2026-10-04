"""Producer-shaped evidence for synthetic suite packages.

These are offline examples, not evidence of an executed chain or Wasm probe.
Boundary reports are the generated synthetic fixtures under
``tests/fixtures/evidence/`` (see ``support/synthetic_evidence.py``); only the
run's transport identity (the selected Gonka SHA) is adapted here. Cargo
examples follow the contract tests and adapters.py at 10662bc. Expected cases
are independent of the verifier.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import hashlib
import json
from xml.etree import ElementTree as ET

from tests.unit.runner.real_fixtures import (
    EVIDENCE_DIR,
    GO_BOUNDARY_EVIDENCE_DIR,
    WASM_ABI_EVIDENCE,
    load_synthetic_evidence,
)
from tests.unit.runner.support.synthetic_evidence import WASM_PLACEHOLDER


def _json_bytes(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def _staged_report(name):
    """A committed boundary report as a run would have written it.

    The ``test_fixture_only`` marker is what the verifiers refuse outright
    (``verify_go_boundary_report`` / ``verify_wasm_abi_report``), so a staged
    package that kept it would fail for a reason unrelated to the behaviour
    under test. It is dropped here, in memory, and only here: the committed
    bytes keep it, and ``test_fixture_artifacts.py`` checks the staged copy
    does not. The ``synthetic_fixture`` block stays: a real producer does not
    write one, but the verifiers ignore unknown keys and keeping it means the
    staged report still names its own provenance.
    """
    report = load_synthetic_evidence(name)
    report.pop("test_fixture_only", None)
    return report


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
    """Return ancillary fixtures and require their paths to match the catalog.

    ``task`` is a catalog task and uses its exact current task ID.
    """
    artifacts = {}
    if task.task_id == "wasm-abi-boundary":
        # Valid empty Wasm module for package transport only; never executed.
        # The committed report is already bound to this placeholder's sha256;
        # it is re-stated here so the package cannot drift from the file.
        wasm = WASM_PLACEHOLDER
        report = _staged_report(WASM_ABI_EVIDENCE)
        report["wasm_sha256"] = hashlib.sha256(wasm).hexdigest()
        artifacts = {
            "a8_query_boundary.wasm": wasm,
            "abi.json": _json_bytes(report),
        }
    elif task.task_id == "go-query-error-classification":
        fixture_dir = EVIDENCE_DIR / GO_BOUNDARY_EVIDENCE_DIR
        report = _staged_report(f"{GO_BOUNDARY_EVIDENCE_DIR}/report.json")
        # The package belongs to this synthetic run; raw events remain verbatim
        # because test_output_sha256 binds their exact bytes.
        report["gonka_sha"] = gonka_sha
        artifacts = {
            relative: (fixture_dir / relative).read_bytes()
            for relative in ("build.log", "raw/go-test.json", "raw/exit-code")
        }
        artifacts["report.json"] = _json_bytes(report)
    elif task.task_id in (
        "contract-network-unconfirmed-policy",
        "contract-claim-expiry-policy",
    ):
        name = {
            "contract-network-unconfirmed-policy": "network_unconfirmed_refund_rejects_early_and_unexpected_system_failures",
            "contract-claim-expiry-policy": "claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow",
        }[task.task_id]
        artifacts[task.task_id + ".log"] = (
            f"running 1 test\ntest contract::tests::{name} ... ok\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 67 filtered out\n"
        ).encode()
    elif task.task_id == "contract-query-fault-policy":
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
