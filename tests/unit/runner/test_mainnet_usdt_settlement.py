"""Verify settlement accounting using a producer-shaped synthetic baseline.

The settlement phase comes from the committed identity-scrubbed fixture via
real_fixtures; post-delivery rejection receipts are generated with seed hashes.
All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import copy
import unittest

from forward_e2e.suite.verifier import _validate_mainnet_usdt_settlement
from tests.unit.runner.real_fixtures import real_claim_settle_phase
from tests.unit.runner.support.synthetic_evidence import synthetic_tx_hash


def settlement_context():
    phase = real_claim_settle_phase()
    accounts = {entry["fault"]["role"]: entry["fault"]["recipient"] for entry in phase["cw20_fault_rollbacks"]}
    config = phase["deal_config"]
    context = {
        "accounts": accounts,
        "contracts": {"deal": config["deal_address"], "cw20": config["settlement_cw20"]},
        "terms": {
            "target_epoch": config["target_epoch"], "budget_micro_usdt": config["buyer_budget_micro_usdt"],
            "price_micro_usdt_per_gnk": config["price_micro_usdt_per_gnk"], "fee_bps": config["fee_bps"],
        },
        "phases": [phase],
    }
    phase["cw20_fault_rollbacks"] = []
    final = {
        "state": copy.deepcopy(phase["after"]["deal_state"]),
        "cw20": {key: phase["after"]["cw20"][key] for key in ("host", "fee_recipient", "buyer", "deal")},
        "payments": {},
    }
    for key, role in (("host", "host"), ("fee", "fee_recipient"), ("buyer", "buyer")):
        amount = phase["expected"]["cw20_deltas"][role]
        final["payments"][key] = {"recipient": accounts[role], "accrued_micro_usdt": str(amount), "paid_micro_usdt": str(amount), "pending_micro_usdt": "0"}
    start = int(phase["settle_repeat"]["attempt"]["height"]) + 1
    phase["withdrawal_repeats"] = [{
        "role": role,
        "attempt": {"tx_hash": synthetic_tx_hash("mainnet-usdt-repeat", index), "height": str(start + index), "code": 5, "codespace": "wasm", "layer": "deliver_tx", "raw_log": "no USDT remains payable for this role"},
        "before": copy.deepcopy(final), "after": copy.deepcopy(final),
    } for index, role in enumerate(("host", "fee", "buyer"))]
    return context


class MainnetUsdtSettlementTests(unittest.TestCase):
    def test_native_summary_three_payouts_and_three_rejected_repeats_reconcile(self):
        self.assertTrue(_validate_mainnet_usdt_settlement(settlement_context()))

    def test_a_changed_native_reward_is_not_compensated_by_matching_reported_totals(self):
        value = copy.deepcopy(settlement_context())
        value["phases"][0]["summary"]["epochPerformanceSummary"]["rewarded_coins"] = "1"
        self.assertFalse(_validate_mainnet_usdt_settlement(value))

    def test_a_missing_host_transfer_receipt_is_not_credited_as_a_payout(self):
        value = copy.deepcopy(settlement_context())
        del value["phases"][0]["withdrawal_txs"]["host"]
        self.assertFalse(_validate_mainnet_usdt_settlement(value))

    def test_a_different_token_in_one_transfer_is_rejected(self):
        value = copy.deepcopy(settlement_context())
        events = value["phases"][0]["withdrawal_txs"]["host"]["events"]
        attributes = next(event["attributes"] for event in events if event["type"] == "wasm")
        next(item for item in attributes if item["key"] == "_contract_address")["value"] = value["contracts"]["deal"]
        self.assertFalse(_validate_mainnet_usdt_settlement(value))

    def test_a_repeat_that_changes_one_paid_obligation_is_rejected(self):
        value = copy.deepcopy(settlement_context())
        value["phases"][0]["withdrawal_repeats"][0]["after"]["payments"]["host"]["paid_micro_usdt"] = "0"
        self.assertFalse(_validate_mainnet_usdt_settlement(value))

    def test_a_checktx_repeat_does_not_prove_execution_in_the_native_keeper(self):
        value = copy.deepcopy(settlement_context())
        value["phases"][0]["withdrawal_repeats"][0]["attempt"]["layer"] = "check_tx"
        self.assertFalse(_validate_mainnet_usdt_settlement(value))

    def test_a_missing_repeat_for_one_role_is_not_credited(self):
        value = copy.deepcopy(settlement_context())
        value["phases"][0]["withdrawal_repeats"].pop()
        self.assertFalse(_validate_mainnet_usdt_settlement(value))


if __name__ == "__main__":
    unittest.main()
