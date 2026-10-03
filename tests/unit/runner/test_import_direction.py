"""Pins the import direction between the suite runner and the E2E layer.

``forward_e2e.execution`` is built on top of ``forward_e2e.suite``; the reverse
edge would be an import cycle. These tests parse every suite module with
``ast`` -- importing them would prove nothing about edges that are only
reached lazily -- and fail on any import of ``forward_e2e.execution`` that is
not one of the two documented exceptions.

The exceptions are stated here deliberately rather than hidden in an allowlist
constant: ``reporter.py`` and ``verifier.py`` import ``file_safety`` from the
execution package. Both edges predate the repository refactoring (they were
``ops/a8/reporter.py`` and ``ops/a8/verifier.py`` importing
``.e2e.file_safety``), and ``file_safety`` depends only on ``execution.errors``
and ``execution.sources`` (which in turn imports ``execution.gitio``), none of
which imports the suite, so no cycle exists at run time. Widening the
exception set, or letting ``file_safety`` grow a suite import, is what this
module is meant to make visible.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]
SUITE_DIR = REPO_ROOT / "forward_e2e" / "suite"
EXECUTION_DIR = REPO_ROOT / "forward_e2e" / "execution"

#: (suite module, execution module) edges that are tolerated today. Each one is
#: a documented exception in ``AGENTS.md`` section 2 and ``docs/architecture.md``.
TOLERATED_REVERSE_EDGES = frozenset(
    {
        ("reporter", "file_safety"),
        ("verifier", "file_safety"),
    }
)

#: Modules the tolerated edge is allowed to pull in transitively
#: (``file_safety`` -> ``errors``, ``sources``; ``sources`` -> ``gitio``). If one
#: of them ever imports the suite, the "no cycle" argument above stops holding.
FILE_SAFETY_CLOSURE = ("file_safety", "errors", "sources", "gitio")


def _imports_of(path: Path) -> list:
    """Return ``(level, module)`` pairs for every import statement in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.level, node.module or ""))
    return found


def _execution_targets(level: int, module: str) -> list:
    """Name the execution modules an import statement reaches, if any."""
    if level == 0 and module.startswith("forward_e2e.execution"):
        rest = module[len("forward_e2e.execution"):].lstrip(".")
        return [rest.split(".")[0] if rest else ""]
    if level == 2 and (module == "execution" or module.startswith("execution.")):
        rest = module[len("execution"):].lstrip(".")
        return [rest.split(".")[0] if rest else ""]
    return []


class SuiteNeverImportsTheExecutionLayerTests(unittest.TestCase):
    def setUp(self):
        self.suite_modules = sorted(p for p in SUITE_DIR.glob("*.py"))
        self.assertTrue(self.suite_modules, f"no suite modules found under {SUITE_DIR}")

    def test_every_suite_module_is_free_of_execution_imports_except_the_two_documented_file_safety_edges(self):
        observed_reverse_edges = set()
        for path in self.suite_modules:
            for level, module in _imports_of(path):
                for target in _execution_targets(level, module):
                    observed_reverse_edges.add((path.stem, target))
        unexpected = observed_reverse_edges - TOLERATED_REVERSE_EDGES
        self.assertEqual(
            unexpected,
            set(),
            msg=(
                "forward_e2e.suite must not import forward_e2e.execution; "
                f"new reverse edges: {sorted(unexpected)}"
            ),
        )

    def test_the_documented_reverse_edges_still_exist_so_the_exception_list_cannot_rot(self):
        """An exception that no longer corresponds to an import must be removed, not kept."""
        observed = set()
        for path in self.suite_modules:
            for level, module in _imports_of(path):
                for target in _execution_targets(level, module):
                    observed.add((path.stem, target))
        self.assertEqual(observed, set(TOLERATED_REVERSE_EDGES))

    def test_file_safety_and_its_dependencies_never_import_the_suite_so_the_tolerated_edge_is_not_a_cycle(self):
        for name in FILE_SAFETY_CLOSURE:
            path = EXECUTION_DIR / f"{name}.py"
            self.assertTrue(path.is_file(), f"missing execution module {path}")
            for level, module in _imports_of(path):
                self.assertFalse(
                    (level == 0 and module.startswith("forward_e2e.suite"))
                    or (level == 2 and (module == "suite" or module.startswith("suite."))),
                    msg=f"{path.name} imports the suite ({module!r}); the file_safety edge would become a cycle",
                )
                if level == 1:
                    self.assertIn(
                        module.split(".")[0],
                        FILE_SAFETY_CLOSURE,
                        msg=f"{path.name} imports {module!r}, which is outside the audited closure",
                    )

    def test_source_snapshot_is_standard_library_only_because_the_harness_loads_it_by_path(self):
        stdlib = set(sys.stdlib_module_names)
        for level, module in _imports_of(SUITE_DIR / "source_snapshot.py"):
            self.assertEqual(level, 0, msg=f"source_snapshot.py uses a relative import of {module!r}")
            top = module.split(".")[0]
            self.assertNotEqual(top, "forward_e2e", msg="source_snapshot.py must not import the package")
            self.assertIn(top, stdlib, msg=f"source_snapshot.py imports a non-stdlib module: {module!r}")


if __name__ == "__main__":
    unittest.main()
