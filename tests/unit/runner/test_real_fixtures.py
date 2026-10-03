"""Regression checks for fixture economics and synthetic fixture isolation.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.runner.real_fixtures import (
    G3_EVIDENCE, NETWORK_MANIFEST_SHA256, REQUIRED_SYNTHETIC_EVIDENCE,
    load_recorded_evidence, real_b3_context, real_claim_settle_phase,
    real_claim_expiry_positive_context, real_network_unconfirmed_context,
    real_r2_scenario_context, real_terminal_release_repeat_phase,
    write_immutable_companion_files,
)


class RealFixtureTests(unittest.TestCase):
    def test_immutable_companions_use_current_network_proof_and_matching_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = write_immutable_companion_files(Path(tmp))
        immutability = json.loads(files["source-immutability.json"])
        manifest = json.loads(files["network/network-manifest.json"])

        self.assertEqual(immutability["schema"], "a8.source-immutability-set/2")
        self.assertEqual(manifest["copy_mode"], "full_working_tree")
        self.assertEqual(immutability["network_root"], manifest["post_run_verification"])
        self.assertEqual(
            hashlib.sha256(files["network/network-manifest.json"]).hexdigest(),
            NETWORK_MANIFEST_SHA256,
        )

    def test_d1_e1_unit_contexts_are_not_live_run_artifacts(self):
        d1 = real_claim_expiry_positive_context()
        e1 = real_network_unconfirmed_context()
        b3 = real_b3_context()
        for context in (d1, e1, b3):
            self.assertIs(context.get("test_fixture_only"), True)
            self.assertTrue(context["run_id"].startswith("synthetic-"))

        for name in REQUIRED_SYNTHETIC_EVIDENCE:
            if name.endswith(".json") and not name.endswith("go-test.json"):
                self.assertIs(load_recorded_evidence(name).get("test_fixture_only"), True, name)

        d1_scenario = d1["scenarios"]["claim-expiry-positive"]
        rejected = next(p for p in d1_scenario["phases"] if p["name"] == "refund_rejected")
        committed = next(p for p in d1_scenario["phases"] if p["name"] == "refund_committed")
        self.assertEqual(d1_scenario["terms"]["target_epoch"], 5)
        self.assertEqual(rejected["observed_epoch"], 6)
        self.assertEqual(committed["observed_epoch"], 7)
        self.assertEqual(rejected["attempt"]["height"], "160")
        self.assertEqual(committed["tx"]["height"], "184")

        e1_scenario = e1["scenarios"]["network-unconfirmed"]
        rejected = next(p for p in e1_scenario["phases"] if p["name"] == "refund_rejected")
        committed = next(p for p in e1_scenario["phases"] if p["name"] == "refund_committed")
        self.assertEqual(e1_scenario["terms"]["target_epoch"], 7)
        self.assertEqual(rejected["observed_epoch"], 9)
        self.assertEqual(committed["observed_epoch"], 10)
        self.assertEqual(rejected["attempt"]["height"], "234")
        self.assertEqual(committed["tx"]["height"], "259")

    def test_terminal_snapshot_preserves_fixture_payouts_and_their_total(self):
        phase = real_terminal_release_repeat_phase()
        fixture = load_recorded_evidence(G3_EVIDENCE)["terminal_snapshot"]
        for snapshot in (phase["before"], phase["after"]):
            self.assertEqual(snapshot["state"], fixture["state"])
            self.assertEqual(snapshot["bank"], fixture["bank_balances"])
            state = snapshot["state"]
            self.assertEqual(int(state["buyer_released_ngonka"]), 100000000000)
            self.assertEqual(
                int(state["buyer_released_ngonka"]) + int(state["host_released_ngonka"]),
                int(state["released_total_ngonka"]),
            )
        phase["before"]["state"]["buyer_released_ngonka"] = "1"
        self.assertEqual(phase["after"]["state"], fixture["state"])
        self.assertEqual(real_terminal_release_repeat_phase()["before"]["state"], fixture["state"])

    def test_withdrawal_failures_preserve_settled_debts_until_successful_payouts(self):
        phase = real_claim_settle_phase()
        previous_height = int(phase["settle_tx"]["height"])
        state_fields = {"host": "host_net_usdt", "fee": "fee_usdt", "buyer": "buyer_refund_usdt"}
        for entry in phase["cw20_fault_rollbacks"]:
            self.assertEqual(entry["before"], entry["after"])
            self.assertEqual(entry["before"]["state"], phase["after"]["deal_state"])
            self.assertEqual(entry["before"]["state"]["status"], "releasing")
            pending = entry["pending_before"]
            self.assertEqual(set(pending), {"host", "fee", "buyer"})
            self.assertEqual(pending, entry["pending_after"])
            for role, field in state_fields.items():
                payment = pending[role]
                self.assertEqual(set(payment), {
                    "recipient", "accrued_micro_usdt", "paid_micro_usdt", "pending_micro_usdt",
                })
                self.assertEqual(payment["accrued_micro_usdt"], entry["before"]["state"][field])
                self.assertEqual(payment["paid_micro_usdt"], "0")
                self.assertEqual(payment["pending_micro_usdt"], payment["accrued_micro_usdt"])
            self.assertEqual(sum(int(p["pending_micro_usdt"]) for p in pending.values()),
                             entry["before"]["cw20"]["deal"])
            target = entry["fault"]
            role = "fee" if target["role"] == "fee_recipient" else target["role"]
            self.assertEqual(pending[role]["recipient"], target["recipient"])
            self.assertEqual(int(pending[role]["pending_micro_usdt"]), target["amount"])
            for key in ("setup_tx", "attempt", "clear_tx"):
                self.assertGreater(int(entry[key]["height"]), previous_height)
                previous_height = int(entry[key]["height"])
        self.assertEqual(set(phase["withdrawal_txs"]), {"host", "fee", "buyer"})
        for role, receipt in phase["withdrawal_txs"].items():
            self.assertEqual(receipt["code"], 0)
            payment = phase["settlement_payments"][role]
            events = receipt["events"]
            self.assertEqual([event["type"] for event in events], [
                "execute", "wasm-usdt_refunded" if role == "buyer" else "wasm-usdt_paid",
                "execute", "wasm",
            ])
            deal_call, payout, token_call, transfer = [
                {a["key"]: a["value"] for a in event["attributes"]}
                for event in events
            ]
            deal = phase["deal_config"]["deal_address"]
            token = phase["deal_config"]["settlement_cw20"]
            self.assertEqual(deal_call["_contract_address"], deal)
            self.assertEqual(payout["_contract_address"], deal)
            self.assertEqual(token_call["_contract_address"], token)
            self.assertEqual(transfer["_contract_address"], token)
            self.assertEqual(payout["recipient"], payment["recipient"])
            self.assertEqual(payout["amount_micro_usdt"], payment["pending_micro_usdt"])
            self.assertEqual(transfer["action"], "transfer")
            self.assertEqual(transfer["from"], deal)
            self.assertEqual(transfer["to"], payment["recipient"])
            self.assertEqual(transfer["amount"], payment["pending_micro_usdt"])
            self.assertGreater(int(receipt["height"]), previous_height)
            previous_height = int(receipt["height"])
        self.assertGreater(int(phase["settle_repeat"]["attempt"]["height"]), previous_height)
        first = phase["cw20_fault_rollbacks"][0]
        first["pending_before"]["host"]["pending_micro_usdt"] = "0"
        self.assertNotEqual(first["pending_before"], first["pending_after"])
        self.assertEqual(first["pending_after"], phase["settlement_payments"])

    def test_separate_r2_fixture_calls_do_not_share_mutable_phase_data(self):
        first = real_r2_scenario_context()
        second = real_r2_scenario_context()
        first["scenarios"]["r2-vested-gift"]["phases"][0].clear()
        self.assertTrue(second["scenarios"]["r2-vested-gift"]["phases"][0])
