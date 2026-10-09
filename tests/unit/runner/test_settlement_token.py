"""Exercise token policy with generated binary bytes and node-shaped documents.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import copy
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forward_e2e.suite import settlement_token as token
from tests.unit.runner.support.synthetic_evidence import synthetic_address, synthetic_tx_hash


class SettlementTokenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = b"synthetic token Wasm bytes"
        self.digest = hashlib.sha256(self.binary).hexdigest()
        self.contract = synthetic_address("settlement-token", "contract")
        self.buyer = synthetic_address("settlement-token", "buyer")
        self.admin = synthetic_address("settlement-token", "admin")
        for name, value in (("MAINNET_WASM_SHA256", self.digest), ("MAINNET_ADDRESS", self.contract), ("MAINNET_CODE_ID", "17")):
            patcher = patch.object(token, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge = {"chain_id": "synthetic-ethereum", "contract_address": "0x" + hashlib.sha256(b"synthetic origin token").hexdigest()[:40]}
        patcher = patch.object(token, "BRIDGE_IDENTITY", bridge)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.docs = {
            "code-info.json": {"code_id": "17", "data_hash": self.digest.upper()},
            "code-info-node1.json": {"code_id": "17", "data_hash": self.digest.upper()},
            "contract-info.json": {
                "address": self.contract, "contract_info": {"code_id": "17", "admin": self.admin},
            },
            "token_info.json": {"data": {**token.TOKEN_METADATA, "total_supply": "200000000"}},
            "bridge_info.json": {"data": dict(token.BRIDGE_IDENTITY)},
            "minter.json": {"data": {"minter": self.admin, "cap": None}},
        }
        (self.root / "mainnet-usdt.wasm").write_bytes(self.binary)
        for name, document in self.docs.items():
            self.write(name, document)

    def write(self, name, document):
        (self.root / name).write_text(json.dumps(document), encoding="utf-8")

    def local_context(self):
        deal = synthetic_address("settlement-token", "deal")
        def tx(index):
            return {"tx_hash": synthetic_tx_hash("settlement-token", index), "height": str(index + 10), "code": 0}
        before, after = {"buyer": 200000000, "deal": 0}, {"buyer": 100000000, "deal": 100000000}
        funded = {"status": "funded", "buyer": self.buyer}
        payload = {
            "source": {"settlement_token_mode": token.MAINNET_MODE, "settlement_token_sha256": self.digest},
            "accounts": {"buyer": self.buyer},
            "contracts": {"cw20": self.contract, "deal": deal},
            "code_ids": {"cw20": "3"},
            "stores": {"cw20": {"sha256": self.digest, "code_id": "3", "tx": tx(1)}},
            "deployments": {"cw20": {"address": self.contract, "tx": tx(2), "contract_info": {"contract_info": {"code_id": "3", "admin": ""}}}},
            "terms": {"budget_micro_usdt": "100000000"},
            "settlement_token": {
                "schema_version": "forward-usdt/local-token/1",
                "asset_identity": token.verify_mainnet_assets(self.root),
                "local_differences": dict(token.LOCAL_DIFFERENCES), "local_controller": self.admin,
                "instantiate_message": token.local_instantiate_message(self.buyer, 200000000, self.admin),
                "code_info": {"code_id": "3", "checksum": self.digest.upper()},
                "metadata_tx": tx(3), "bridge_info": dict(token.BRIDGE_IDENTITY),
                "token_info": {**token.TOKEN_METADATA, "total_supply": "200000000"},
                "minter_query": {"data": None}, "initial_buyer_balance": "200000000",
            },
            "bootstrap": {"fund_tx": tx(5), "cw20_before": before, "cw20_after": after, "deal_state": funded, "funding_rejections": {}},
        }
        def events(*entries):
            return [{"type": kind, "attributes": [{"key": key, "value": value} for key, value in attributes.items()]} for kind, attributes in entries]
        payload["stores"]["cw20"]["tx"]["events"] = events(("store_code", {"code_id": "3", "code_checksum": self.digest}))
        payload["deployments"]["cw20"]["tx"]["events"] = events(("instantiate", {"_contract_address": self.contract, "code_id": "3"}))
        payload["settlement_token"]["metadata_tx"]["events"] = events(
            ("execute", {"_contract_address": self.contract}),
            ("wasm", {"_contract_address": self.contract, "method": "update_metadata", **{key: str(value) for key, value in token.TOKEN_METADATA.items()}}),
        )
        payload["bootstrap"]["fund_tx"]["events"] = events(*(
            ("wasm-deal_funded", {"_contract_address": deal, "deal": deal, "buyer": self.buyer, "buyer_budget_micro_usdt": "100000000"}),
            ("wasm", {"_contract_address": self.contract, "action": "send", "from": self.buyer, "to": deal, "amount": "100000000"}),
        ))
        for name, index, status, balances, amount, marker in (
            ("wrong_amount", 4, "open", before, 99999999, "funding amount must be exactly"),
            ("duplicate", 6, "funded", after, 100000000, "cannot fund deal in state"),
        ):
            snapshot = {"state": funded if status == "funded" else {"status": "open", "buyer": None}, "cw20": copy.deepcopy(balances)}
            payload["bootstrap"]["funding_rejections"][name] = {
                "contract": self.contract, "sender": self.buyer,
                "message": {"send": {"contract": deal, "amount": str(amount), "msg": base64.b64encode(b'{"fund":{}}').decode()}},
                "attempt": {**tx(index), "code": 5, "codespace": "wasm", "layer": "deliver_tx", "raw_log": marker},
                "before": snapshot, "after": copy.deepcopy(snapshot),
            }
        return payload

    def verify_local(self, payload):
        return token.verify_local_token_evidence(payload, expected_mode=token.MAINNET_MODE, asset_identity=token.verify_mainnet_assets(self.root))

    def test_local_receipts_bind_code_metadata_and_two_atomic_send_rejections(self):
        self.assertIsNone(self.verify_local(self.local_context()))

    def test_a_store_receipt_cannot_substitute_for_the_token_instantiation(self):
        value = copy.deepcopy(self.local_context())
        value["deployments"]["cw20"]["tx"] = copy.deepcopy(value["stores"]["cw20"]["tx"])
        self.assertIn("operation receipts", self.verify_local(value))

    def test_a_store_receipt_cannot_substitute_for_the_metadata_update(self):
        value = copy.deepcopy(self.local_context())
        value["settlement_token"]["metadata_tx"] = copy.deepcopy(value["stores"]["cw20"]["tx"])
        self.assertIn("operation receipts", self.verify_local(value))

    def test_metadata_for_a_different_token_does_not_prove_this_tokens_initialization(self):
        value = copy.deepcopy(self.local_context())
        value["settlement_token"]["metadata_tx"]["events"][1]["attributes"][0]["value"] = self.buyer
        self.assertIn("operation receipts", self.verify_local(value))

    def test_a_metadata_update_after_funding_is_rejected_as_inconsistent_bootstrap_evidence(self):
        value = copy.deepcopy(self.local_context())
        value["settlement_token"]["metadata_tx"]["height"] = "100"
        self.assertIn("operation receipts", self.verify_local(value))

    def test_a_store_event_for_another_code_is_not_compensated_by_the_code_query(self):
        value = copy.deepcopy(self.local_context())
        value["stores"]["cw20"]["tx"]["events"][0]["attributes"][0]["value"] = "4"
        self.assertIn("operation receipts", self.verify_local(value))

    def test_a_missing_mode_declaration_cannot_downgrade_a_locked_mainnet_token_run(self):
        value = copy.deepcopy(self.local_context())
        del value["source"]["settlement_token_mode"]
        self.assertIn("run lock", self.verify_local(value))

    def test_a_changed_local_code_checksum_is_not_compensated_by_the_mainnet_download(self):
        value = copy.deepcopy(self.local_context())
        value["settlement_token"]["code_info"]["checksum"] = hashlib.sha256(b"different local code").hexdigest()
        self.assertIn("pinned Wasm", self.verify_local(value))

    def test_a_changed_local_initial_supply_is_rejected(self):
        value = copy.deepcopy(self.local_context())
        value["settlement_token"]["token_info"]["total_supply"] = "200000001"
        self.assertIn("initial supply mismatch", self.verify_local(value))

    def test_a_checktx_rejection_does_not_prove_native_send_rollback(self):
        value = copy.deepcopy(self.local_context())
        value["bootstrap"]["funding_rejections"]["wrong_amount"]["attempt"]["layer"] = "check_tx"
        self.assertIn("atomic rollback", self.verify_local(value))

    def test_a_failed_send_that_changes_one_balance_is_rejected(self):
        value = copy.deepcopy(self.local_context())
        value["bootstrap"]["funding_rejections"]["wrong_amount"]["after"]["cw20"]["buyer"] -= 1
        self.assertIn("atomic rollback", self.verify_local(value))

    def test_a_missing_duplicate_funding_attempt_is_not_credited(self):
        value = copy.deepcopy(self.local_context())
        del value["bootstrap"]["funding_rejections"]["duplicate"]
        self.assertIn("incomplete", self.verify_local(value))

    def test_mainnet_token_selection_rejects_scenarios_requiring_test_only_commands(self):
        environment = {token.ENV_TOKEN_MODE: token.MAINNET_MODE}
        self.assertEqual(token.validate_selection(environment, ["terminal-release-repeat"]), token.MAINNET_MODE)
        with self.assertRaises(token.SettlementTokenError):
            token.validate_selection(environment, ["funded-claim"])

    def test_protobuf_base64_code_checksums_are_decoded_without_changing_identity(self):
        self.assertEqual(token.code_checksum({"checksum": base64.b64encode(bytes.fromhex(self.digest)).decode()}), self.digest)

    def test_a_missing_local_checksum_is_not_compensated_by_the_mainnet_download_hash(self):
        value = copy.deepcopy(self.local_context())
        del value["settlement_token"]["code_info"]["checksum"]
        self.assertIn("checksum is missing", self.verify_local(value))

    def test_binary_and_both_code_observations_are_bound_to_the_contract_identity(self):
        result = token.verify_mainnet_assets(self.root)
        self.assertEqual(result["wasm_sha256"], self.digest)
        self.assertEqual(result["mainnet_contract"], self.contract)
        self.assertEqual(result["mainnet_code_id"], "17")
        self.assertEqual(result["scope"], "historical-mainnet-binary-identity")
        for name in self.docs:
            self.assertEqual(result["observation_sha256"][name], hashlib.sha256((self.root / name).read_bytes()).hexdigest())

    def test_a_changed_binary_is_rejected_even_when_the_observations_are_unchanged(self):
        (self.root / "mainnet-usdt.wasm").write_bytes(self.binary + b"changed")
        with self.assertRaisesRegex(token.SettlementTokenError, "checksum mismatch"):
            token.verify_mainnet_assets(self.root)

    def test_one_disagreeing_node_checksum_cannot_be_compensated_by_the_other_node(self):
        value = copy.deepcopy(self.docs["code-info-node1.json"])
        value["data_hash"] = hashlib.sha256(b"different synthetic binary").hexdigest()
        self.write("code-info-node1.json", value)
        with self.assertRaisesRegex(token.SettlementTokenError, "observation mismatch"):
            token.verify_mainnet_assets(self.root)

    def test_a_contract_migration_to_a_different_code_id_is_rejected(self):
        value = copy.deepcopy(self.docs["contract-info.json"])
        value["contract_info"]["code_id"] = "18"
        self.write("contract-info.json", value)
        with self.assertRaisesRegex(token.SettlementTokenError, "contract observation mismatch"):
            token.verify_mainnet_assets(self.root)

    def test_a_missing_bridge_observation_is_not_treated_as_agreement(self):
        (self.root / "bridge_info.json").unlink()
        with self.assertRaisesRegex(token.SettlementTokenError, "missing token asset"):
            token.verify_mainnet_assets(self.root)

    def test_a_wrong_decimal_count_is_rejected_before_a_local_token_can_be_prepared(self):
        value = copy.deepcopy(self.docs["token_info.json"])
        value["data"]["decimals"] = 18
        self.write("token_info.json", value)
        with self.assertRaisesRegex(token.SettlementTokenError, "metadata mismatch"):
            token.verify_mainnet_assets(self.root)

    def test_a_different_ethereum_token_is_rejected_even_with_the_same_symbol(self):
        value = copy.deepcopy(self.docs["bridge_info.json"])
        value["data"]["contract_address"] = "0x" + "42" * 20
        self.write("bridge_info.json", value)
        with self.assertRaisesRegex(token.SettlementTokenError, "bridge identity mismatch"):
            token.verify_mainnet_assets(self.root)

    def test_malformed_node_observations_are_reported_as_policy_refusals(self):
        value = copy.deepcopy(self.docs["code-info.json"])
        del value["data_hash"]
        self.write("code-info.json", value)
        with self.assertRaises(token.SettlementTokenError) as caught:
            token.verify_mainnet_assets(self.root)
        self.assertEqual(caught.exception.code, "SETTLEMENT_TOKEN_INVALID")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_local_initial_balances_do_not_require_a_mainnet_minter_or_wallet(self):
        value = token.local_instantiate_message(self.buyer, 200000000, self.admin)
        self.assertEqual(value["initial_balances"], [{"address": self.buyer, "amount": "200000000"}])
        self.assertIsNone(value["mint"])
        self.assertEqual(value["admin"], self.admin)
        self.assertEqual(value["chain_id"], token.BRIDGE_IDENTITY["chain_id"])

    def test_a_misspelled_token_mode_never_falls_back_to_a_test_cw20(self):
        self.assertEqual(token.token_mode({}), token.TEST_MODE)
        self.assertEqual(token.token_mode({token.ENV_TOKEN_MODE: token.MAINNET_MODE}), token.MAINNET_MODE)
        with self.assertRaises(token.SettlementTokenError):
            token.token_mode({token.ENV_TOKEN_MODE: "mainnet-usdtt"})

    def test_a_boolean_cannot_be_interpreted_as_a_local_initial_token_balance(self):
        with self.assertRaises(token.SettlementTokenError):
            token.local_instantiate_message(self.buyer, True, self.admin)


if __name__ == "__main__":
    unittest.main()
