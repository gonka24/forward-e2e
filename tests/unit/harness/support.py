"""Shared offline test doubles and harness loaders for scripts/tests.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[3] / "scripts" / "acceptance_harness.py"
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))

if "a8_acceptance" in sys.modules:
    a8 = sys.modules["a8_acceptance"]
else:
    SPEC = importlib.util.spec_from_file_location("a8_acceptance", MODULE_PATH)
    assert SPEC is not None and SPEC.loader is not None
    a8 = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = a8
    sys.modules["acceptance_harness"] = a8
    SPEC.loader.exec_module(a8)


#: Every variable that parameterises the harness's provenance expectations,
#: plus the removed overlay variables (which the harness refuses). Tests clear
#: all of them so an ambient shell value can never decide a test's outcome.
EXPECTATION_ENV_NAMES = (
    a8.ENV_EXPECTED_GONKA_SHA,
    a8.ENV_EXPECTED_PROTO_SHA,
    a8.ENV_EXPECTED_RUNTIME,
    a8.ENV_EVIDENCE_MODEL,
    *a8.LEGACY_OVERLAY_ENVIRONMENT,
)


class FakeRunner:
    """Deterministic queue-driven command runner double for a8_acceptance."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, *, input_text=None, timeout=None):
        self.calls.append((list(argv), input_text, timeout))
        if not self.results:
            raise AssertionError(f"unexpected command: {argv}")
        return self.results.pop(0)


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Construct a subprocess.CompletedProcess with an empty argv."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def env_pairs(command) -> dict[str, str]:
    """The VAR=value entries of an ``env ...`` argv, as a dictionary."""
    pairs = {}
    for item in command:
        if item in ("bash", "java") or item.endswith("/java"):
            break
        if "=" in item and not item.startswith("-"):
            key, _, value = item.partition("=")
            pairs[key] = value
    return pairs


def version_output(*, commit: str, wasmd: str = "v0.54.2", wasmvm: str = "v2.2.4") -> str:
    """Synthetic ``inferenced version --long`` output."""
    return "\n".join(
        [
            "name: inferenced",
            "server_name: inferenced",
            f"commit: {commit}",
            "go: go version go1.23.5 linux/amd64",
            "cosmos_sdk_version: v0.53.0",
            "build_deps:",
            f"- github.com/CosmWasm/wasmd@{wasmd}",
            f"- github.com/CosmWasm/wasmvm/v2@{wasmvm}",
        ]
    )


class StubGonka:
    """Minimal Gonka client stub exposing chain_id and status()."""

    def __init__(self, chain_id, status):
        self.chain_id = chain_id
        self._status = status

    def status(self):
        return self._status


class BankRollbackGonka:
    """Gonka double simulating bank state during failed tx rollback checks."""

    def __init__(self, attempt):
        self.chain_id = a8.DEFAULT_CHAIN_ID
        self.attempt = attempt

    def status(self):
        return {"node_info": {"network": a8.DEFAULT_CHAIN_ID}, "sync_info": {"catching_up": False}}

    def transfer_restriction_status(self):
        return {"is_active": True, "remaining_blocks": 10, "current_block_height": 100}

    def smart(self, _contract, _query):
        return {
            "status": "releasing",
            "released_total_ngonka": "0",
            "buyer_released_ngonka": "0",
            "host_released_ngonka": "0",
            "gnk_release_policy": "host_only",
        }

    def cw20_balance(self, _contract, _address):
        return 0

    def bank_balance(self, address):
        return 100 if address == "deal" else 0

    def tx_attempt(self, *_args, **_kwargs):
        return dict(self.attempt)


class PaymentChain:
    """Synthetic settlement and USDT withdrawal state machine for harness tests."""

    def __init__(self, path):
        self.path = path
        self.context = {
            "chain": {"chain_id": "test"},
            "terms": {
                "target_epoch": 5,
                "budget_micro_usdt": "100000000",
                "price_micro_usdt_per_gnk": "1000000",
                "fee_bps": 150,
            },
            "accounts": {"host": "host", "buyer": "buyer", "fee_recipient": "fees"},
            "contracts": {"deal": "deal", "cw20": "token", "factory": "factory"},
            "key_names": {"host": "host"},
            "phases": [],
        }
        self.summary = {
            "epochPerformanceSummary": {
                "claimed": True,
                "epoch_index": "5",
                "participant_id": "host",
                "earned_coins": "80000000000",
                "rewarded_coins": "0",
            }
        }
        # Independent arithmetic: 80 GNK of work at 1 USDT/GNK consumes
        # 80 of the 100 USDT budget. A 1.5% fee is 1.2 USDT, leaving 78.8
        # for the host and a 20 USDT refund. Never call the oracle here: its
        # mistakes must disagree with this fixture, not redefine the chain.
        self.expected = {
            "status": "releasing",
            "work_ngonka": 80000000000,
            "reward_ngonka": 0,
            "total_claim_ngonka": 80000000000,
            "funded_capacity_ngonka": 100000000000,
            "buyer_entitlement_ngonka": 80000000000,
            "host_entitlement_ngonka": 0,
            "gnk_release_policy": {
                "proportional": {
                    "buyer_share_numerator": 80000000000,
                    "share_denominator": 80000000000,
                }
            },
            "gross_usdt": 80000000,
            "fee_usdt": 1200000,
            "host_net_usdt": 78800000,
            "buyer_refund_usdt": 20000000,
            "cw20_deltas": {"host": 78800000, "fee_recipient": 1200000, "buyer": 20000000},
            "deal_outflow": 100000000,
        }
        # Producer shape: contract.rs::state_response and marketplace-api's
        # deal.rs::StateResponse at ce54dd462fade3c031777756a48f087fc44062de.
        # CosmWasm Uint128/Uint256 fields serialize as decimal strings.
        self.settled_state = {
            "status": "releasing",
            "refund_reason": None,
            "buyer": "buyer",
            "recipient_locked": True,
            "work_ngonka": "80000000000",
            "reward_ngonka": "0",
            "total_claim_ngonka": "80000000000",
            "buyer_entitlement_ngonka": "80000000000",
            "host_entitlement_ngonka": "0",
            "gnk_release_policy": {
                "proportional": {
                    "buyer_share_numerator": "80000000000",
                    "share_denominator": "80000000000",
                }
            },
            "released_total_ngonka": "0",
            "buyer_released_ngonka": "0",
            "host_released_ngonka": "0",
            "gross_usdt": "80000000",
            "fee_usdt": "1200000",
            "host_net_usdt": "78800000",
            "buyer_refund_usdt": "20000000",
        }
        self.state = {"status": "locked", "buyer": "buyer"}
        self.balances = {"deal": 100000000, "host": 0, "buyer": 0, "fees": 0}
        self.roles = {
            "host": ("host", 78800000),
            "fee": ("fees", 1200000),
            "buyer": ("buyer", 20000000),
        }
        self.pending = {role: amount for role, (_, amount) in self.roles.items()}
        self.blocked = "fee"
        self.calls = []
        self.query_failure = False
        self.payout_checks = []
        a8.write_object(path, self.context)

    def tx(self, *args, **kwargs):
        return {"txhash": "CLAIM", "code": 0, "height": "9"}

    def query_json(self, module, *args):
        return copy.deepcopy(self.summary) if module == "inference" else {}

    def bank_balance(self, address):
        return 0

    def cw20_balance(self, token, address):
        return self.balances[address]

    def smart(self, address, msg):
        if "state" in msg:
            return copy.deepcopy(self.state)
        if "usdt_payments" in msg:
            if self.query_failure:
                raise a8.AcceptanceError("query unavailable after settlement")
            return {
                role: {
                    "recipient": recipient,
                    "accrued_micro_usdt": str(amount),
                    "pending_micro_usdt": str(self.pending[role]),
                    "paid_micro_usdt": str(amount - self.pending[role]),
                }
                for role, (recipient, amount) in self.roles.items()
            }
        return {}

    def execute(self, node, key, deal, msg, **kwargs):
        self.calls.append(msg)
        if "settle_claim" in msg:
            if self.state["status"] != "locked":
                raise a8.AcceptanceError("cannot settle claim in state")
            self.state = copy.deepcopy(self.settled_state)
            return {"txhash": "SETTLED", "code": 0, "height": "10"}
        role = msg["withdraw_usdt"]["role"]
        # Observe the actual file before each broadcast, not merely mock calls.
        phases = a8.load_object(self.path)["phases"]
        self.payout_checks.append(copy.deepcopy(phases))
        if role == self.blocked:
            raise a8.AcceptanceError("recipient blocked")
        amount = self.pending[role]
        if not amount:
            raise a8.AcceptanceError("already paid")
        self.balances[self.roles[role][0]] += amount
        self.balances["deal"] -= amount
        self.pending[role] = 0
        return {"txhash": role.upper(), "code": 0, "height": "11"}

    def tx_attempt(self, *args, **kwargs):
        message = json.loads(args[-1])
        if "withdraw_usdt" in message:
            assert message["withdraw_usdt"]["role"] == self.blocked
            log = "a8 injected cw20 transfer failure"
        else:
            assert "settle_claim" in message
            log = "cannot settle claim in state Releasing"
        return {
            "layer": "deliver_tx",
            "tx_hash": "REJECTED",
            "height": "12",
            "code": 5,
            "codespace": "wasm",
            "raw_log": log,
        }
