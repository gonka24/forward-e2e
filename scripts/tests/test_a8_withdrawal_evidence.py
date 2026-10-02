"""Durable evidence across confirmed settlement and partial delivery failures.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import copy
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

try:
    from scripts.tests.support import PaymentChain, a8
except ImportError:
    from support import PaymentChain, a8


class WithdrawalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "context.json"
        self.chain = PaymentChain(self.path)
        for name, kwargs in (("DockerGonka", {"return_value": self.chain}),
                             ("assert_chain", {}), ("assert_deal_terms", {})):
            patcher = patch.object(a8, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = Namespace(context=str(self.path), reward_epoch=5, reward_seed=1,
                              host_node="host", fault_retry=False, cw20_fault_positions=None)

    def phases(self):
        return a8.load_object(self.path)["phases"]

    def test_partial_failure_persists_then_retry_after_completed_verifies_full_oracle(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "pending for: fee"):
            a8.claim_settle(self.args)
        phases = self.phases()
        checkpoint = next(p for p in phases if p["name"] == "settlement_committed")
        self.assertEqual(checkpoint["settle_tx"]["tx_hash"], "SETTLED")
        self.assertEqual(checkpoint["before"]["cw20"]["deal"], 100000000)
        attempts = [p for p in phases if p["name"] == "usdt_withdrawal_attempt"]
        self.assertEqual([(p["role"], p["status"]) for p in attempts],
                         [("host", "confirmed"), ("fee", "failed"), ("buyer", "confirmed")])
        self.assertEqual(attempts[0]["tx"]["tx_hash"], "HOST")
        self.assertEqual(attempts[1]["error"], "recipient blocked")
        self.assertFalse(any(p["name"] == "settlement_delivery_verified" for p in phases))
        for index, snapshot in enumerate(self.chain.payout_checks):
            self.assertTrue(any(p["name"] == "settlement_committed" for p in snapshot))
            self.assertEqual(sum(p["name"] == "usdt_withdrawal_attempt" for p in snapshot), index)
        self.chain.blocked = None
        self.chain.state["status"] = "completed"
        a8.withdraw_usdt(Namespace(context=str(self.path)))
        self.assertEqual(self.chain.calls[-1], {"withdraw_usdt": {"role": "fee"}})
        self.assertEqual(sum("settle_claim" in call for call in self.chain.calls), 1)
        verified = next(p for p in self.phases() if p["name"] == "settlement_delivery_verified")
        self.assertEqual(verified["actual"]["cw20_deltas"], self.chain.expected["cw20_deltas"])
        self.assertEqual(verified["actual"]["deal_outflow"], 100000000)
        self.assertEqual(self.phases()[:len(phases)], phases)

    def test_failed_retry_is_saved_and_does_not_claim_verification(self):
        with self.assertRaises(a8.AcceptanceError):
            a8.claim_settle(self.args)
        with self.assertRaises(a8.AcceptanceError):
            a8.withdraw_usdt(Namespace(context=str(self.path)))
        self.assertEqual(self.phases()[-1]["role"], "fee")
        self.assertEqual(self.phases()[-1]["status"], "failed")
        self.assertFalse(any(p["name"] == "settlement_delivery_verified" for p in self.phases()))

    def test_query_failure_after_settlement_keeps_checkpoint(self):
        self.chain.query_failure = True
        with self.assertRaisesRegex(a8.AcceptanceError, "query unavailable"):
            a8.claim_settle(self.args)
        self.assertEqual(self.phases()[0]["settle_tx"]["tx_hash"], "SETTLED")
        self.assertEqual(len(self.chain.calls), 1)

    def test_disk_failure_stops_before_next_broadcast(self):
        original = a8.write_object
        def fail_on_attempt(path, value):
            if value["phases"][-1]["name"] == "usdt_withdrawal_attempt":
                raise OSError("disk full")
            original(path, value)
        with patch.object(a8, "write_object", side_effect=fail_on_attempt):
            with self.assertRaisesRegex(OSError, "disk full"):
                a8.claim_settle(self.args)
        self.assertEqual(self.chain.calls, [{"settle_claim": {}}, {"withdraw_usdt": {"role": "host"}}])
        self.assertEqual(self.phases()[0]["name"], "settlement_committed")

    def test_success_still_runs_original_oracle_and_repeat_check(self):
        self.chain.blocked = None
        a8.claim_settle(self.args)
        self.assertEqual(self.phases()[-1]["name"], "claim_settle")
        self.assertEqual(self.phases()[-1]["expected"], self.chain.expected)
        self.assertEqual(self.phases()[-1]["settle_repeat"]["proof"]["contract_error"], "InvalidSettlementState")

    def test_retry_cannot_pass_oracle_with_wrong_balance(self):
        with self.assertRaises(a8.AcceptanceError):
            a8.claim_settle(self.args)
        self.chain.blocked = None
        self.chain.balances["host"] -= 1
        with self.assertRaisesRegex(a8.AcceptanceError, "CW20 deltas differ"):
            a8.withdraw_usdt(Namespace(context=str(self.path)))
        self.assertEqual(self.phases()[-1]["status"], "confirmed")
        self.assertFalse(any(p["name"] == "settlement_delivery_verified" for p in self.phases()))

    def test_a_settled_state_with_one_extra_claim_unit_is_rejected_by_the_independent_oracle(self):
        self.chain.blocked = None
        # Derive the negative case from the positive chain response, changing
        # just the total. The expected result and native summary stay intact.
        self.chain.settled_state["total_claim_ngonka"] = "80000000001"
        with self.assertRaisesRegex(a8.AcceptanceError, "settlement state differs"):
            a8.claim_settle(self.args)
        self.assertFalse(any(p["name"] == "settlement_delivery_verified" for p in self.phases()))

    def test_merged_fault_positions_target_withdrawals_after_settlement(self):
        self.args.cw20_fault_positions = [1, 2, 3]
        configured = []
        def configure(gonka, context, recipient):
            self.assertEqual(self.chain.state["status"], "releasing")
            configured.append(recipient)
            self.chain.blocked = next((role for role, (addr, _) in self.chain.roles.items()
                                       if addr == recipient), None)
            return {"txhash": "CONFIG", "code": 0, "height": "10"}
        with patch.object(a8, "configure_cw20_transfer_failure", side_effect=configure):
            a8.claim_settle(self.args)
        self.assertEqual(configured, ["host", None, "fees", None, "buyer", None])
        faults = self.phases()[-1]["cw20_fault_rollbacks"]
        self.assertEqual(len(faults), 3)
        for fault in faults:
            self.assertEqual(fault["before"], fault["after"])
            self.assertEqual(fault["before"]["state"]["status"], "releasing")
            self.assertEqual(fault["pending_before"]["fee"]["pending_micro_usdt"], "1200000")

    def test_named_scenario_preserves_evidence_and_retries_only_its_debt(self):
        context = a8.load_object(self.path)
        context["scenarios"] = {"example": copy.deepcopy(context)}
        a8.write_object(self.path, context)
        with self.assertRaisesRegex(a8.AcceptanceError, "pending for: fee"):
            a8.settle_scenario(Namespace(context=str(self.path), name="example"))
        partial = a8.load_object(self.path)["scenarios"]["example"]["phases"]
        self.assertEqual(partial[0]["name"], "settlement_committed")
        self.assertEqual([p["role"] for p in partial[1:]], ["host", "fee", "buyer"])
        self.chain.blocked = None
        a8.withdraw_usdt(Namespace(context=str(self.path), name="example"))
        saved = a8.load_object(self.path)
        self.assertEqual(saved["phases"], [])
        self.assertEqual(saved["scenarios"]["example"]["phases"][:len(partial)], partial)
        self.assertTrue(any(p["name"] == "settlement_delivery_verified"
                            for p in saved["scenarios"]["example"]["phases"]))


if __name__ == "__main__":
    unittest.main()
