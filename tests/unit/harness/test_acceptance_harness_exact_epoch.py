"""Exact-epoch E1 lock scenario tests for scripts/acceptance_harness.py.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from tests.unit.harness.support import a8 as a
except ImportError:
    from support import a8 as a


class ExactELockTests(unittest.TestCase):
    def test_exact_epoch_and_immutable_accounting(self):
        self.run_case([4, 5, 5, 5], succeeds=True)

    def test_missed_epoch_does_not_execute(self):
        self.run_case([6, 6], succeeds=False)

    def test_crossing_inclusion_epoch_is_rejected(self):
        self.run_case([5, 5, 6], succeeds=False, executed=True)

    def run_case(self, epochs, succeeds, executed=False):
        state = {"status": "funded", "recipient_locked": False, "buyer": "buyer"}
        snap = {"state": state, "cw20": {"deal": 10},
                "foreign_cw20": {"deal": 0},
                "bank_ngonka": {r: 0 for r in ("host", "buyer", "deal")}}
        after = copy.deepcopy(snap)
        after["state"].update(status="locked", recipient_locked=True)
        scenario = {"terms": {"target_epoch": 5}, "accounts": {"host": "host", "buyer": "buyer", "fee_recipient": "fee"},
                    "contracts": {"deal": "deal"}, "phases": [],
                    "prepared": {"deal_state": state, "deal_config": {"host": "host"}}}
        g = Mock()
        g.key_address.return_value = "caller"
        g.query_json.return_value = {"entries": [{"epoch": 5, "recipient": "deal"}]}
        g.smart.return_value = {"host": "host"}
        g.execute.return_value = {"height": "100", "tx_hash": "A"*64, "code": 0, "events": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"context.json"
            a.write_object(path, {"chain": {"chain_id": "test"}, "scenarios": {"case": scenario}})
            with patch.object(a, "DockerGonka", return_value=g), patch.object(a, "assert_chain"), patch.object(
                a, "epoch_observation", side_effect=[{"epoch": e, "height": 100} for e in epochs]), patch.object(
                a, "scenario_financial_snapshot", side_effect=[snap, after]), patch.object(a.time, "sleep"):
                if succeeds:
                    a.lock_exact_e_scenario(SimpleNamespace(context=str(path), name="case"))
                    phase = a.load_object(path)["scenarios"]["case"]["phases"][-1]
                    self.assertEqual(phase["name"], "lock_exact_e")
                    self.assertEqual(phase["epoch_bracket"]["epoch"], 5)
                    g.execute.assert_called_once()
                else:
                    with self.assertRaises(a.AcceptanceError):
                        a.lock_exact_e_scenario(SimpleNamespace(context=str(path), name="case"))
                    self.assertEqual(g.execute.call_count, int(executed))
