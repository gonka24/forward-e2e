"""Regression checks for fixture economics and synthetic fixture isolation.

The D1/E1 checks below derive every expected value from the scenario's own
declarations (``terms.target_epoch``, ``terms.budget_micro_usdt``, the
``accounts`` and ``contracts`` maps, the epoch brackets) rather than from the
literal heights the fixture module happens to use. A test that repeats the
module's constants only proves the module equals itself; a test that recomputes
the relations proves the fixture is the mutually consistent document the
verifier's cross-field rules assume, so that a negative test which breaks one
relation fails for that reason and not because the positive was already
inconsistent.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.runner.real_fixtures import (
    G3_EVIDENCE, NETWORK_MANIFEST_SHA256, REQUIRED_SYNTHETIC_EVIDENCE,
    load_synthetic_evidence, real_b3_context, real_claim_settle_phase,
    real_claim_expiry_positive_context, real_claim_expiry_zero_context,
    real_network_unconfirmed_context, real_r2_scenario_context,
    real_terminal_release_repeat_phase, write_immutable_companion_files,
)


def _phases(scenario, name):
    return [phase for phase in scenario["phases"] if phase["name"] == name]


def _only_phase(scenario, name):
    phases = _phases(scenario, name)
    assert len(phases) == 1, (name, len(phases))
    return phases[0]


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

    def test_every_in_memory_live_context_and_committed_document_is_marked_test_only_with_a_synthetic_run_id(self):
        """The marker is what stops a fixture from being ingested as a run; the run id is what names it synthetic."""
        for name, context in (
            ("claim-expiry-positive", real_claim_expiry_positive_context()),
            ("claim-expiry-zero", real_claim_expiry_zero_context()),
            ("network-unconfirmed", real_network_unconfirmed_context()),
            ("b3", real_b3_context()),
        ):
            with self.subTest(context=name):
                self.assertIs(context.get("test_fixture_only"), True)
                self.assertTrue(context["run_id"].startswith("synthetic-"), context["run_id"])

        for name in REQUIRED_SYNTHETIC_EVIDENCE:
            if name.endswith(".json") and not name.endswith("go-test.json"):
                with self.subTest(document=name):
                    self.assertIs(load_synthetic_evidence(name).get("test_fixture_only"), True)

    # -- shared refund-scenario relations ------------------------------------

    def assert_refund_phases_follow_the_declared_window(self, scenario, *, rejected_offset, committed_offset,
                                                        rejected_reason, committed_reason):
        """The relations every D1/E1 refund scenario must satisfy, computed from its own terms.

        ``rejected_offset``/``committed_offset`` are the epochs after
        ``target_epoch`` at which the catalog expects the early rejection and
        the commit (E+1/E+2 for claim expiry, E+2/E+3 for the emergency
        deadline); everything else -- heights, balances, reasons -- is read
        from the scenario and compared with itself.
        """
        target = scenario["terms"]["target_epoch"]
        budget = scenario["terms"]["budget_micro_usdt"]
        rejected = _only_phase(scenario, "refund_rejected")
        committed = _only_phase(scenario, "refund_committed")

        # Epoch placement follows the declared target, not a literal.
        self.assertEqual(rejected["observed_epoch"], target + rejected_offset)
        self.assertEqual(committed["observed_epoch"], target + committed_offset)
        for phase, receipt in ((rejected, rejected["attempt"]), (committed, committed["tx"])):
            bracket = phase["epoch_bracket"]
            self.assertEqual(bracket["epoch"], phase["observed_epoch"])
            self.assertIs(bracket["same_epoch"], True)
            self.assertLessEqual(bracket["before_height"], bracket["tx_height"])
            self.assertLessEqual(bracket["tx_height"], bracket["after_height"])
            # The receipt was included at the bracketed height, as a string
            # because that is how the node renders it.
            self.assertEqual(int(receipt["height"]), bracket["tx_height"])
        self.assertLess(rejected["epoch_bracket"]["after_height"], committed["epoch_bracket"]["before_height"])

        # The early attempt fails closed and moves nothing.
        self.assertEqual(rejected["expected_reason"], rejected_reason)
        self.assertEqual(rejected["actual_reason"]["reason"], rejected_reason)
        self.assertEqual(rejected["actual_reason"]["state_status"], rejected["before"]["state"]["status"])
        self.assertEqual(rejected["attempt"]["code"], 5)
        self.assertEqual(rejected["attempt"]["codespace"], "wasm")
        self.assertIn(rejected["actual_reason"]["matched_contract_error"], rejected["attempt"]["raw_log"])
        self.assertEqual(rejected["before"], rejected["after"])
        self.assertEqual(rejected["before"]["state"]["status"], "locked")
        self.assertEqual(rejected["before"]["cw20"]["deal"], budget)

        # The commit refunds exactly the budget from the Deal to the Buyer.
        before, after = committed["before"], committed["after"]
        self.assertEqual(committed["reason"], committed_reason)
        self.assertEqual(committed["tx"]["code"], 0)
        self.assertEqual(before["state"]["status"], "locked")
        self.assertEqual(after["state"]["status"], "refunded")
        self.assertEqual(after["state"]["refund_reason"], committed_reason)
        self.assertEqual(before["cw20"]["deal"], budget)
        self.assertEqual(after["cw20"]["deal"], 0)
        self.assertEqual(after["cw20"]["buyer"] - before["cw20"]["buyer"], budget)
        self.assertEqual(after["cw20"]["fee_recipient"], before["cw20"]["fee_recipient"])
        self.assertEqual(after["bank_ngonka"], before["bank_ngonka"])
        self.assertEqual(committed["expected"], {
            "status": "refunded",
            "buyer_refund": budget,
            "host_usdt": 0,
            "fee_usdt": 0,
            "native_transfers": 0,
        })
        self.assertIsNone(committed["cw20_fault_rollback"])

        # The terminal retries come after the commit, in order, and both fail.
        heights = [int(committed["tx"]["height"]),
                   int(committed["terminal_repeat"]["height"]),
                   int(committed["competing_settlement"]["height"])]
        self.assertEqual(heights, sorted(heights))
        self.assertEqual(len(set(heights)), len(heights))
        for retry in (committed["terminal_repeat"], committed["competing_settlement"]):
            self.assertEqual((retry["layer"], retry["code"], retry["codespace"]), ("deliver_tx", 5, "wasm"))

        # Both snapshots name the scenario's own parties.
        for snapshot in (rejected["before"], before, after):
            self.assertEqual(snapshot["state"]["buyer"], scenario["accounts"]["buyer"])
            self.assertEqual(snapshot["state"]["host"], scenario["accounts"]["host"])
            self.assertIs(snapshot["state"]["recipient_locked"], True)

    def test_the_d1_refund_phases_are_consistent_with_the_claim_expiry_window_the_scenario_declares(self):
        for name, context in (
            ("claim-expiry-positive", real_claim_expiry_positive_context()),
            ("claim-expiry-zero", real_claim_expiry_zero_context()),
        ):
            with self.subTest(scenario=name):
                scenario = context["scenarios"][name]
                self.assertEqual(scenario["name"], name)
                target = scenario["terms"]["target_epoch"]
                self.assert_refund_phases_follow_the_declared_window(
                    scenario,
                    rejected_offset=1, committed_offset=2,
                    rejected_reason="too_early", committed_reason="claim_expiry",
                )
                rejected = _only_phase(scenario, "refund_rejected")
                committed = _only_phase(scenario, "refund_committed")
                # The contract's own words agree with the terms: the window
                # opens two epochs after the target and the attempt was one.
                self.assertEqual(
                    rejected["actual_reason"]["matched_contract_error"],
                    f"claim-expiry window has not opened: current epoch {target + 1}, "
                    f"target epoch {target}, first allowed epoch {target + 2}",
                )
                self.assertEqual(committed["after"]["state"]["buyer_refund_usdt"],
                                 str(scenario["terms"]["budget_micro_usdt"]))

                # Each native precondition is the reading taken just before the
                # refund attempt that follows it: same epoch, and its height is
                # the bracket's before_height.
                preconditions = _phases(scenario, "native_unclaimed_precondition")
                self.assertEqual(len(preconditions), 2)
                for precondition, refund in zip(preconditions, (rejected, committed)):
                    self.assertEqual(precondition["observation"]["epoch"], refund["observed_epoch"])
                    self.assertEqual(precondition["observation"]["height"], refund["epoch_bracket"]["before_height"])
                    self.assertEqual(precondition["target_epoch"], target)
                    summary = precondition["summary"]["epochPerformanceSummary"]
                    self.assertEqual(summary["epoch_index"], target)
                    self.assertEqual(summary["participant_id"], scenario["accounts"]["host"])
                    self.assertNotIn("claimed", summary)  # proto3 default omitted, i.e. unclaimed
                    self.assertIs(precondition["claimed"], False)
                    self.assertEqual(precondition["total_ngonka"], summary["rewarded_coins"])
                    # Positive and zero branches are the same scenario with one
                    # fact flipped: the sign of the unclaimed total.
                    positive = precondition["total_ngonka"] > 0
                    self.assertEqual(name == "claim-expiry-positive", positive)
                    self.assertIs(precondition["positive_total_required"], positive)
                    self.assertIs(precondition["zero_total_required"], not positive)
                    (entry,) = precondition["recipient_query"]["entries"]
                    self.assertEqual(entry, {
                        "epoch": target,
                        "recipient": scenario["contracts"]["deal"],
                        "participant": scenario["accounts"]["host"],
                    })
                # The order in the document is the order on the chain.
                self.assertEqual(
                    [phase["name"] for phase in scenario["phases"]],
                    ["native_unclaimed_precondition", "refund_rejected",
                     "native_unclaimed_precondition", "refund_committed"],
                )
                self.assertNotEqual(scenario["accounts"]["buyer"], scenario["accounts"]["host"])

    def test_the_e1_refund_phases_are_consistent_with_the_emergency_deadline_the_scenario_declares(self):
        context = real_network_unconfirmed_context()
        scenario = context["scenarios"]["network-unconfirmed"]
        target = scenario["terms"]["target_epoch"]
        self.assert_refund_phases_follow_the_declared_window(
            scenario,
            rejected_offset=2, committed_offset=3,
            rejected_reason="network_unconfirmed_too_early", committed_reason="network_unconfirmed",
        )
        rejected = _only_phase(scenario, "refund_rejected")
        committed = _only_phase(scenario, "refund_committed")

        # The catalog's stated limitation: Buyer and Host are one account, so
        # every snapshot's host CW20 row is the buyer's row seen twice.
        self.assertEqual(scenario["accounts"]["buyer"], scenario["accounts"]["host"])
        for snapshot in (rejected["before"], rejected["after"], committed["before"], committed["after"]):
            self.assertEqual(snapshot["cw20"]["host"], snapshot["cw20"]["buyer"])

        # Each native absence is the reading taken just before the refund
        # attempt that follows it, at the offset it says it was taken at.
        absences = _phases(scenario, "native_summary_absent")
        self.assertEqual(len(absences), 2)
        for absence, refund in zip(absences, (rejected, committed)):
            self.assertEqual(absence["target_epoch"], target)
            self.assertEqual(absence["observation"]["epoch"], target + absence["expected_offset"])
            self.assertEqual(absence["observation"]["epoch"], refund["observed_epoch"])
            self.assertEqual(absence["observation"]["height"], refund["epoch_bracket"]["before_height"])
            self.assertEqual(absence["host"], scenario["accounts"]["host"])
            query = absence["native_query"]
            self.assertEqual((query["returncode"], query["grpc_code"]), (1, "NotFound"))
            self.assertIs(query["exact_native_handler_not_found"], True)
            self.assertEqual(query["stdout"], "")
            (entry,) = absence["recipient_query"]["entries"]
            self.assertEqual(entry, {
                "epoch": target,
                "recipient": scenario["contracts"]["deal"],
                "participant": scenario["accounts"]["host"],
            })
            self.assertEqual(absence["expected"], {
                "summary_absent": True, "transport_available": True, "exact_recipient": True,
            })
        self.assertEqual([absence["expected_offset"] for absence in absences], [2, 3])
        self.assertEqual(
            [phase["name"] for phase in scenario["phases"]],
            ["native_summary_absent", "refund_rejected", "native_summary_absent", "refund_committed"],
        )

    def test_terminal_snapshot_preserves_fixture_payouts_and_their_total(self):
        phase = real_terminal_release_repeat_phase()
        fixture = load_synthetic_evidence(G3_EVIDENCE)["terminal_snapshot"]
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
