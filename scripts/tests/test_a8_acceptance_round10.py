"""Round 10 provenance and Linux runtime invocation tests for a8_acceptance.py.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import ast
import unittest
from pathlib import Path


try:
    from scripts.tests.support import (
        MODULE_PATH,
        a8,
    )
except ImportError:
    from support import (
        MODULE_PATH,
        a8,
    )


class BootstrapSourceProvenanceTests(unittest.TestCase):
    """The bootstrap ``source`` dictionary must record three distinct facts.

    The dictionary is written deep inside the bootstrap phase, which only runs
    against a live chain, so it is read structurally from the source instead of
    being executed.
    """

    @staticmethod
    def source_dict_node():
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        wanted = {"gonka_source_sha", "gonka_sha", "evidence_model"}
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if wanted <= keys:
                found.append(node)
        return found

    def test_the_live_context_source_records_the_requested_commit_model_and_runtime_but_no_prepared_commit(self):
        nodes = self.source_dict_node()
        self.assertEqual(len(nodes), 1, "expected exactly one live-context source dictionary")
        node = nodes[0]

        values = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant):
                values[key.value] = value

        # Merely checking the key would accept None or copied expectations.
        runtime = values["runtime"]
        self.assertIsInstance(runtime, ast.Name)
        self.assertEqual(runtime.id, "runtime_identity")
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        bootstrap = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "bootstrap"
        )
        assignments = [
            node.value for node in ast.walk(bootstrap)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == runtime.id
                    for target in node.targets)
        ]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(
            ast.dump(assignments[0]),
            ast.dump(ast.parse("parse_runtime_identity(gonka.binary_version())", mode="eval").body),
        )

        def called_function(name):
            expression = values[name]
            self.assertIsInstance(expression, ast.Call)
            self.assertIsInstance(expression.func, ast.Name)
            return expression.func.id

        # The requested commit is recorded twice, under the new explicit name
        # and under the historical one, so older readers keep working.
        self.assertEqual(called_function("gonka_source_sha"), "expected_gonka_sha")
        self.assertEqual(called_function("gonka_sha"), "expected_gonka_sha")
        # The immutable-source model has no prepared commit: the snapshot is
        # exactly the requested one, so the key must not come back.
        self.assertNotIn("gonka_prepared_sha", values)
        self.assertEqual(called_function("evidence_model"), "evidence_model")


class HarnessSelectionTests(unittest.TestCase):
    """``A8_HARNESS`` must name the runner's own harness, never the target's.

    The run-live behaviour (the env Testermint receives) is exercised end to
    end in ``test_a8_immutable_run_live.py``; this pins the constant itself.
    """

    def test_the_harness_constant_is_this_very_file(self):
        self.assertEqual(Path(a8.HARNESS_SCRIPT_PATH), MODULE_PATH)
        self.assertEqual(Path(a8.HARNESS_SCRIPT_PATH).name, "a8_acceptance.py")
        self.assertTrue(Path(a8.HARNESS_SCRIPT_PATH).is_absolute())


if __name__ == "__main__":
    unittest.main()
