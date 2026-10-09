"""Pinned settlement-token assets and local initialization policy.

This module is stdlib-only and can also be loaded by path by Testermint's
standalone harness. Downloaded mainnet bytes are used directly: the selected
Gonka repository's wrapped-token artifact is a different binary.
"""

from __future__ import annotations

import hashlib
import json
import base64
import re
from pathlib import Path
from typing import Any, Mapping

ENV_TOKEN_MODE = "E2E_SETTLEMENT_TOKEN"
TEST_MODE = "test-cw20"
MAINNET_MODE = "mainnet-usdt"
MAINNET_WASM_SHA256 = "5833f840d5fde0f5eb179b09a08cc8b7c2cff8da86df647a92f63917f1f31aca"
MAINNET_CODE_ID = "114"
MAINNET_ADDRESS = "gonka15ggwj9un6qrmu4nj5ev6l7kpdcr00td03ff2mmj4cyhl8u8vjd2qnl3hgk"
BRIDGE_IDENTITY = {
    "chain_id": "ethereum",
    "contract_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
}
TOKEN_METADATA = {"name": "Tether USD", "symbol": "USDT", "decimals": 6}
UNSUPPORTED_MAINNET_SCENARIOS = frozenset({
    "funded-claim", "usdt-withdrawal-failure-recovery", "funded-routing-refunds",
})
LOCAL_DIFFERENCES = {
    "balances": "fresh-local-initial-balances",
    "mint": "none-requested-at-instantiate",
    "token_state_admin": "local-genesis-controller",
    "wasm_admin": "none",
    "mainnet_storage_reproduced": False,
    "source_binary_equivalence_claimed": False,
}


class SettlementTokenError(ValueError):
    """A token-selection or binary-provenance refusal, never a fallback."""

    code = "SETTLEMENT_TOKEN_INVALID"
    exit_code = 2


def token_mode(environment: Mapping[str, str]) -> str:
    """Refuse misspellings instead of quietly testing a different token."""
    mode = environment.get(ENV_TOKEN_MODE, TEST_MODE)
    if mode not in (TEST_MODE, MAINNET_MODE):
        raise SettlementTokenError(f"unsupported {ENV_TOKEN_MODE}: {mode!r}")
    return mode


def validate_selection(environment: Mapping[str, str], scenarios: list[str]) -> str:
    mode = token_mode(environment)
    rejected = sorted(set(scenarios) & UNSUPPORTED_MAINNET_SCENARIOS)
    if mode == MAINNET_MODE and rejected:
        raise SettlementTokenError(
            "mainnet USDT has no test-CW20 transfer-fault commands; unsupported scenarios: "
            + ", ".join(rejected)
        )
    return mode


def code_checksum(observation: Mapping[str, Any]) -> str:
    """Accept the CLI's hex or protobuf JSON bytes encoding, never guess a hash."""
    if not isinstance(observation, Mapping):
        raise SettlementTokenError("local code observation is malformed")
    # QueryCodeInfoResponse uses checksum. CodeResponse's nested data_hash is
    # a different RPC response (used only by the saved mainnet download).
    raw = observation.get("checksum")
    if not isinstance(raw, str):
        raise SettlementTokenError("local code checksum is missing")
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return raw.lower()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SettlementTokenError("local code checksum encoding is malformed") from exc
    if len(decoded) != 32:
        raise SettlementTokenError("local code checksum is not 32 bytes")
    return decoded.hex()


def _included(tx: Any) -> bool:
    return (
        isinstance(tx, Mapping)
        and re.fullmatch(r"[0-9a-fA-F]{64}", str(tx.get("tx_hash", ""))) is not None
        and str(tx.get("height", "")).isdigit() and int(tx["height"]) > 0
        and str(tx.get("code", "")) == "0"
    )


def _rejected(attempt: Mapping[str, Any], marker: str) -> bool:
    return (
        attempt.get("layer") == "deliver_tx" and attempt.get("codespace") == "wasm"
        and re.fullmatch(r"[0-9a-fA-F]{64}", str(attempt.get("tx_hash", ""))) is not None
        and str(attempt.get("height", "")).isdigit() and int(attempt["height"]) > 0
        and str(attempt.get("code", "")).isdigit() and int(attempt["code"]) > 0
        and marker in str(attempt.get("raw_log", "")).lower()
    )


def _event(tx: Mapping[str, Any], kind: str, address: str | None = None) -> dict[str, str] | None:
    found = []
    for event in tx.get("events", []):
        if event.get("type") != kind:
            continue
        attributes = event.get("attributes", [])
        values = {entry["key"]: str(entry["value"]) for entry in attributes}
        if len(values) != len(attributes):
            return None
        if address is None or values.get("_contract_address") == address:
            found.append(values)
    return found[0] if len(found) == 1 else None


def verify_funding_rejections(payload: Mapping[str, Any]) -> str | None:
    """Bind native rejected Sends to the same successfully funded Deal."""
    try:
        bootstrap = payload["bootstrap"]
        cases = bootstrap["funding_rejections"]
        if set(cases) != {"wrong_amount", "duplicate"} or not _included(bootstrap["fund_tx"]):
            return "USDT funding rejection receipts are incomplete"
        budget = int(payload["terms"]["budget_micro_usdt"])
        before, after = bootstrap["cw20_before"], bootstrap["cw20_after"]
        funded_event = _event(bootstrap["fund_tx"], "wasm-deal_funded", payload["contracts"]["deal"])
        send_event = _event(bootstrap["fund_tx"], "wasm", payload["contracts"]["cw20"])
        if (
            budget <= 1 or set(before) != {"buyer", "deal"} or set(after) != set(before)
            or int(before["buyer"]) - int(after["buyer"]) != budget
            or int(after["deal"]) - int(before["deal"]) != budget
            or bootstrap["deal_state"].get("status") != "funded"
            or bootstrap["deal_state"].get("buyer") != payload["accounts"]["buyer"]
            or not funded_event or not send_event
            or funded_event.get("deal") != payload["contracts"]["deal"]
            or funded_event.get("buyer") != payload["accounts"]["buyer"]
            or funded_event.get("buyer_budget_micro_usdt") != str(budget)
            or send_event.get("action") != "send" or send_event.get("from") != payload["accounts"]["buyer"]
            or send_event.get("to") != payload["contracts"]["deal"] or send_event.get("amount") != str(budget)
        ):
            return "USDT bootstrap does not prove exact-budget funding"
        hashes = {bootstrap["fund_tx"]["tx_hash"]}
        for name, status, amount, marker, balances in (
            ("wrong_amount", "open", budget - 1, "funding amount must be exactly", before),
            ("duplicate", "funded", budget, "cannot fund deal in state", after),
        ):
            case = cases[name]
            attempt = case["attempt"]
            send = case["message"]["send"]
            if (
                not _rejected(attempt, marker) or attempt["tx_hash"] in hashes
                or case["before"] != case["after"]
                or case["before"]["state"].get("status") != status
                or case["before"]["cw20"] != balances
                or (name == "duplicate" and case["before"]["state"] != bootstrap["deal_state"])
                or (name == "wrong_amount" and case["before"]["state"].get("buyer") is not None)
                or case.get("sender") != payload["accounts"]["buyer"]
                or case.get("contract") != payload["contracts"]["cw20"]
                or send.get("contract") != payload["contracts"]["deal"]
                or str(send.get("amount")) != str(amount)
                or json.loads(base64.b64decode(send["msg"], validate=True)) != {"fund": {}}
            ):
                return f"USDT {name} Send does not prove native atomic rollback"
            height, funded_height = int(attempt["height"]), int(bootstrap["fund_tx"]["height"])
            if (name == "wrong_amount" and height > funded_height) or (name == "duplicate" and height < funded_height):
                return "USDT funding rejection order disagrees with the included funding tx"
            hashes.add(attempt["tx_hash"])
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return f"invalid USDT funding rejection receipt: {exc}"
    return None


def verify_local_token_evidence(
    payload: Mapping[str, Any], *, expected_mode: str | None = None,
    asset_identity: Mapping[str, Any] | None = None,
) -> str | None:
    """One producer/consumer check used by live and offline deployment grading.

    The locked mode is supplied by the E2E layer so deleting the declaration
    cannot downgrade a mainnet-token run to historical test-CW20 evidence.
    """
    try:
        source = payload.get("source", {})
        if not isinstance(source, Mapping):
            return "mainnet-USDT source declaration is missing" if expected_mode == MAINNET_MODE else None
        mode = token_mode({ENV_TOKEN_MODE: source.get("settlement_token_mode", TEST_MODE)})
        if expected_mode is not None and mode != expected_mode:
            return "settlement token mode disagrees with the run lock"
        receipt = payload.get("settlement_token")
        if mode == TEST_MODE:
            if receipt is not None:
                return "test-CW20 mode cannot declare mainnet token evidence"
            return None
        if not isinstance(receipt, Mapping) or receipt.get("schema_version") != "forward-usdt/local-token/1":
            return "mainnet-USDT mode requires a local token receipt"
        expected_asset = asset_identity if asset_identity is not None else verify_mainnet_assets(
            Path(__file__).resolve().parents[2] / "harness/settlement_token"
        )
        if receipt.get("asset_identity") != expected_asset:
            return "mainnet token asset identity differs from the pinned runner assets"
        if receipt.get("local_differences") != LOCAL_DIFFERENCES:
            return "local USDT initialization differences are missing or changed"
        token_address = payload["contracts"]["cw20"]
        store = payload["stores"]["cw20"]
        deployment = payload["deployments"]["cw20"]
        if (
            source.get("settlement_token_sha256") != MAINNET_WASM_SHA256
            or store.get("sha256") != MAINNET_WASM_SHA256
            or not _included(store.get("tx")) or not _included(deployment.get("tx"))
            or not _included(receipt.get("metadata_tx"))
            or deployment.get("address") != token_address
            or code_checksum(receipt["code_info"]) != MAINNET_WASM_SHA256
        ):
            return "local USDT deployment does not prove the pinned Wasm"
        # Success alone cannot identify an operation: a store receipt must not
        # substitute for instantiation or metadata execution in offline proof.
        store_tx, instantiate_tx, metadata_tx = store["tx"], deployment["tx"], receipt["metadata_tx"]
        transactions = (store_tx, instantiate_tx, metadata_tx, payload["bootstrap"]["fund_tx"])
        store_event = _event(store_tx, "store_code")
        instantiate_event = _event(instantiate_tx, "instantiate", token_address)
        metadata_event = _event(metadata_tx, "wasm", token_address)
        if (
            not all(_included(tx) for tx in transactions)
            or len({tx["tx_hash"].lower() for tx in transactions}) != len(transactions)
            or [int(tx["height"]) for tx in transactions] != sorted(int(tx["height"]) for tx in transactions)
            or not store_event or store_event.get("code_checksum", "").lower() != MAINNET_WASM_SHA256
            or store_event.get("code_id") != str(payload["code_ids"]["cw20"])
            or not instantiate_event or instantiate_event.get("code_id") != str(payload["code_ids"]["cw20"])
            or not _event(metadata_tx, "execute", token_address)
            or not metadata_event or metadata_event.get("method") != "update_metadata"
            or any(metadata_event.get(key) != str(value) for key, value in TOKEN_METADATA.items())
        ):
            return "local USDT operation receipts are not bound to ordered store, instantiate and metadata transactions"
        info = deployment["contract_info"]
        info = info.get("contract_info", info)
        code = receipt["code_info"]
        if (
            str(info.get("code_id")) != str(payload["code_ids"]["cw20"])
            or str(code.get("code_id")) != str(payload["code_ids"]["cw20"])
            or str(store.get("code_id")) != str(payload["code_ids"]["cw20"])
            or info.get("admin") not in (None, "")
        ):
            return "local USDT contract is not bound to the stored code and admin policy"
        init = receipt["instantiate_message"]
        initial = init["initial_balances"]
        if not isinstance(initial, list) or len(initial) != 1:
            return "local USDT initial balance receipt is malformed"
        amount = int(initial[0]["amount"])
        if init != local_instantiate_message(payload["accounts"]["buyer"], amount, receipt["local_controller"]):
            return "local USDT initialization message is inconsistent"
        if receipt.get("bridge_info") != BRIDGE_IDENTITY:
            return "local USDT bridge identity differs from Ethereum USDT"
        token_info = receipt["token_info"]
        if (
            any(token_info.get(key) != value for key, value in TOKEN_METADATA.items())
            or str(token_info.get("total_supply")) != str(amount)
            or receipt.get("minter_query") != {"data": None}
            or str(receipt.get("initial_buyer_balance")) != str(amount)
        ):
            return "local USDT metadata, minter or initial supply mismatch"
        funding_error = verify_funding_rejections(payload)
        if funding_error is not None:
            return funding_error
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return f"invalid local USDT receipt: {exc}"
    return None


def _read_regular(root: Path, relative: str) -> bytes:
    # A link in any component could substitute bytes outside the locked tree.
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise SettlementTokenError(f"token asset is a symlink: {relative}")
    if not current.is_file():
        raise SettlementTokenError(f"missing token asset: {relative}")
    return current.read_bytes()


def verify_mainnet_assets(root: Path) -> dict[str, Any]:
    """Tie the binary to both saved node responses and its contract identity.

    These are historical mainnet observations, not local execution proof or a
    claim that the contract still has this code after a future migration.
    """
    root = Path(root)
    if root.is_symlink():
        raise SettlementTokenError("token asset root is a symlink")
    names = (
        "mainnet-usdt.wasm", "code-info.json", "code-info-node1.json",
        "contract-info.json", "token_info.json", "bridge_info.json", "minter.json",
    )
    raw = {name: _read_regular(root, name) for name in names}
    digest = hashlib.sha256(raw["mainnet-usdt.wasm"]).hexdigest()
    if digest != MAINNET_WASM_SHA256:
        raise SettlementTokenError("mainnet USDT Wasm checksum mismatch")
    try:
        docs = {name: json.loads(value) for name, value in raw.items() if name.endswith(".json")}
        for name in ("code-info.json", "code-info-node1.json"):
            code = docs[name]
            if str(code["code_id"]) != MAINNET_CODE_ID or code["data_hash"].lower() != digest:
                raise SettlementTokenError(f"mainnet code observation mismatch: {name}")
        contract = docs["contract-info.json"]
        if contract["address"] != MAINNET_ADDRESS or str(contract["contract_info"]["code_id"]) != MAINNET_CODE_ID:
            raise SettlementTokenError("mainnet contract observation mismatch")
        info = docs["token_info.json"]["data"]
        if any(info.get(key) != value for key, value in TOKEN_METADATA.items()):
            raise SettlementTokenError("mainnet token metadata mismatch")
        if docs["bridge_info.json"]["data"] != BRIDGE_IDENTITY:
            raise SettlementTokenError("mainnet bridge identity mismatch")
        if not docs["minter.json"]["data"]["minter"]:
            raise SettlementTokenError("mainnet minter observation missing")
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise SettlementTokenError("malformed mainnet token observation") from exc
    return {
        "mode": MAINNET_MODE,
        "wasm_sha256": digest,
        "mainnet_contract": MAINNET_ADDRESS,
        "mainnet_code_id": MAINNET_CODE_ID,
        "observation_sha256": {
            name: hashlib.sha256(value).hexdigest() for name, value in raw.items()
            if name.endswith(".json")
        },
        "mainnet_wasm_admin": contract["contract_info"]["admin"],
        "mainnet_minter": docs["minter.json"]["data"],
        "scope": "historical-mainnet-binary-identity",
    }


def local_instantiate_message(buyer: str, amount: int, admin: str) -> dict[str, Any]:
    """Create local test funds without reproducing or spending mainnet balances.

    The wrapped token accepts initial balances. Mint is disabled locally and
    the local controller is recorded as token-state admin; metadata is updated
    by that controller before funding. Wasm migration admin is separate.
    """
    if not buyer or not admin or isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise SettlementTokenError("local USDT requires buyer, admin and a positive integer balance")
    return {
        **BRIDGE_IDENTITY,
        "initial_balances": [{"address": buyer, "amount": str(amount)}],
        "mint": None,
        "admin": admin,
    }
