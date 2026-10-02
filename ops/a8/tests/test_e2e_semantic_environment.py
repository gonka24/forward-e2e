"""Regression tests for ``ops.a8.e2e.executor.apply_semantic_environment``.

A replay is only a replay if it runs in the environment the plan recorded. The
planner freezes variables matching ``SEMANTIC_ENV_PREFIXES``, except operational
variables, into the lock; execution is where that promise is either kept or
quietly broken. These tests pin the three parts of the contract: saved values
are restored over whatever the caller's shell held, ambient semantic variables
the lock never saw are *removed* (``A8_EXPECTED_PROTO_SHA`` is the dangerous
one -- it moves the recorded ABI provenance), and operational variables, which
only say where things live, are left alone. A lock that cannot describe an
environment faithfully -- a ``semantic_environment`` that is not a mapping, or a
value that is not a string -- is refused instead of being coerced.

Most tests pass an explicit environment mapping. Tests of the process mapping
and execution order isolate ``os.environ`` with ``mock.patch.dict``.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.a8.e2e.errors import LockIntegrityError
from ops.a8.e2e.executor import ExecutionRequest, apply_semantic_environment, execute_plan
from ops.a8.e2e.planner import (
    HARNESS_FILES,
    NETWORK_FILES,
    OPERATIONAL_ENV_NAMES,
    SEMANTIC_ENV_PREFIXES,
    VERIFIER_FILES,
    RunnerLayout,
)
from ops.a8.e2e.runlock import BuildManifest
from ops.a8.tests.support.fakes import make_test_lock as _make_lock

#: A variable that carries proof-relevant meaning and matches a semantic
#: prefix, but is not operational: exactly the class that must not survive.
DANGEROUS_AMBIENT = "A8_EXPECTED_PROTO_SHA"


def _lock_with_environment(saved):
    """A real ``RunLock`` whose semantic inputs carry the given environment."""
    semantic_inputs = {
        "semantic_environment": saved,
        "operational_environment_names": list(OPERATIONAL_ENV_NAMES),
        "note": "synthetic fixture",
    }
    return _make_lock(semantic_inputs=semantic_inputs)


class ApplySemanticEnvironmentTests(unittest.TestCase):
    """The planned environment replaces the caller's, not the other way round."""

    def test_every_saved_semantic_value_is_restored_over_the_current_shell(self):
        lock = _lock_with_environment(
            {"A8_PROOF_LEVEL": "native", "GONKA_CHAIN_ID": "gonka-e2e"}
        )
        environ = {"A8_PROOF_LEVEL": "smoke", "GONKA_CHAIN_ID": "somebody-elses-chain"}

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ["A8_PROOF_LEVEL"], "native")
        self.assertEqual(environ["GONKA_CHAIN_ID"], "gonka-e2e")
        self.assertEqual(record["restored"], ["A8_PROOF_LEVEL", "GONKA_CHAIN_ID"])
        self.assertEqual(
            sorted(record["overridden_from_shell"]),
            ["A8_PROOF_LEVEL", "GONKA_CHAIN_ID"],
        )
        self.assertEqual(record["cleared_ambient"], [])

    def test_a_value_the_shell_never_had_is_restored_without_being_called_an_override(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {}

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ, {"A8_PROOF_LEVEL": "native"})
        self.assertEqual(record["restored"], ["A8_PROOF_LEVEL"])
        self.assertEqual(record["overridden_from_shell"], [])

    def test_a_shell_value_that_already_matches_the_plan_is_not_reported_as_overridden(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {"A8_PROOF_LEVEL": "native"}

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ["A8_PROOF_LEVEL"], "native")
        self.assertEqual(record["restored"], ["A8_PROOF_LEVEL"])
        self.assertEqual(record["overridden_from_shell"], [])

    def test_an_ambient_semantic_variable_absent_from_the_lock_is_deleted(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {
            "A8_PROOF_LEVEL": "native",
            DANGEROUS_AMBIENT: "0" * 64,
            "TESTERMINT_EXTRA_FLAGS": "--go-wild",
        }

        record = apply_semantic_environment(lock, environ=environ)

        self.assertNotIn(DANGEROUS_AMBIENT, environ)
        self.assertNotIn("TESTERMINT_EXTRA_FLAGS", environ)
        self.assertEqual(
            record["cleared_ambient"], [DANGEROUS_AMBIENT, "TESTERMINT_EXTRA_FLAGS"]
        )

    def test_an_ambient_variable_matching_every_semantic_prefix_is_cleared(self):
        lock = _lock_with_environment({})
        environ = {f"{prefix}AMBIENT": "set-by-the-shell" for prefix in SEMANTIC_ENV_PREFIXES}
        expected = sorted(environ)

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ, {})
        self.assertEqual(record["cleared_ambient"], expected)

    def test_operational_variables_are_left_untouched(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {name: f"value-of-{name}" for name in OPERATIONAL_ENV_NAMES}

        record = apply_semantic_environment(lock, environ=environ)

        for name in OPERATIONAL_ENV_NAMES:
            self.assertEqual(environ[name], f"value-of-{name}")
            self.assertNotIn(name, record["cleared_ambient"])
            self.assertNotIn(name, record["restored"])

    def test_variables_outside_the_semantic_prefixes_are_left_untouched(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {"PATH": "/usr/bin", "HOME": "/synthetic/home", "LANG": "C.UTF-8"}

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ["PATH"], "/usr/bin")
        self.assertEqual(environ["HOME"], "/synthetic/home")
        self.assertEqual(environ["LANG"], "C.UTF-8")
        self.assertEqual(record["cleared_ambient"], [])

    def test_a_lock_without_a_semantic_environment_still_clears_ambient_variables(self):
        # The helper's default semantic_inputs carry no semantic_environment at
        # all: the plan recorded no semantic variables, so none may appear now.
        lock = _make_lock()
        environ = {DANGEROUS_AMBIENT: "0" * 64, "PATH": "/usr/bin"}

        record = apply_semantic_environment(lock, environ=environ)

        self.assertEqual(environ, {"PATH": "/usr/bin"})
        self.assertEqual(record["restored"], [])
        self.assertEqual(record["cleared_ambient"], [DANGEROUS_AMBIENT])

    def test_a_semantic_environment_that_is_not_a_mapping_is_a_lock_integrity_error(self):
        for saved in (["A8_PROOF_LEVEL=native"], [], "", 0, False, None):
            with self.subTest(saved=saved):
                lock = _lock_with_environment(saved)
                environ = {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64}
                before = dict(environ)
                messages = []

                with self.assertRaises(LockIntegrityError) as cm:
                    apply_semantic_environment(lock, environ=environ, emit=messages.append)

                self.assertIn("semantic_environment is not a mapping", str(cm.exception))
                self.assertEqual(cm.exception.details.get("type"), type(saved).__name__)
                self.assertEqual(environ, before)
                self.assertEqual(messages, [])

    def test_a_non_string_semantic_value_is_a_lock_integrity_error(self):
        # Stringifying it would write something like "7" or "{'a': 1}" into a
        # child's environment and present it as the planned value.
        lock = _lock_with_environment(
            {"A8_PROOF_LEVEL": "native", "GONKA_BLOCK_BUDGET": 7}
        )
        environ = {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64}
        before = dict(environ)
        messages = []

        with self.assertRaises(LockIntegrityError) as cm:
            apply_semantic_environment(lock, environ=environ, emit=messages.append)

        self.assertIn(
            "A semantic environment value in the lock is not a string", str(cm.exception)
        )
        self.assertEqual(
            cm.exception.details, {"name": "GONKA_BLOCK_BUDGET", "type": "int"}
        )
        self.assertEqual(environ, before)
        self.assertEqual(messages, [])

    def test_invalid_names_and_nul_values_are_rejected_before_any_process_environment_changes(self):
        positive = {"A8_PROOF_LEVEL": "native", "GONKA_CHAIN_ID": "gonka-e2e"}
        cases = [
            ({"A8_PROOF_LEVEL": "native", name: positive["GONKA_CHAIN_ID"]}, "name")
            for name in ("", "GONKA_BAD=NAME", "GONKA_BAD\0NAME", 7)
        ]
        cases.append(({**positive, "GONKA_CHAIN_ID": "bad\0value"}, "value"))
        for saved, invalid_part in cases:
            with self.subTest(saved=saved):
                lock = _lock_with_environment(saved)
                messages = []
                # A plain dict accepts NULs and invalid names. Exercise the real
                # process mapping so the fixture cannot hide an OS-level error.
                with mock.patch.dict(
                    os.environ,
                    {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64},
                    clear=True,
                ):
                    before = dict(os.environ)
                    with self.assertRaises(LockIntegrityError) as cm:
                        apply_semantic_environment(lock, emit=messages.append)
                    self.assertIn(invalid_part, str(cm.exception))
                    self.assertEqual(dict(os.environ), before)
                    self.assertEqual(messages, [])

    def test_every_restoration_and_every_clearing_is_explained_to_the_operator(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        environ = {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64}

        messages = []
        apply_semantic_environment(lock, environ=environ, emit=messages.append)

        joined = "\n".join(messages)
        self.assertIn("Restoring semantic variable A8_PROOF_LEVEL", joined)
        self.assertIn(f"Clearing ambient semantic variable {DANGEROUS_AMBIENT}", joined)

    def test_the_returned_record_explains_why_the_shell_was_overruled(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})

        record = apply_semantic_environment(lock, environ={})

        self.assertIn("re-parameterise a replay", record["note"])
        self.assertEqual(
            sorted(record), ["cleared_ambient", "note", "overridden_from_shell", "restored"]
        )


class ApplySemanticEnvironmentDefaultTargetTests(unittest.TestCase):
    """Without an explicit mapping the function works on the real environment."""

    def test_the_default_environment_target_is_the_process_environment(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})

        with mock.patch.dict(
            os.environ,
            {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64},
            clear=False,
        ):
            record = apply_semantic_environment(lock)

            self.assertEqual(os.environ["A8_PROOF_LEVEL"], "native")
            self.assertNotIn(DANGEROUS_AMBIENT, os.environ)
            self.assertIn("A8_PROOF_LEVEL", record["overridden_from_shell"])
            self.assertIn(DANGEROUS_AMBIENT, record["cleared_ambient"])


class ExecutionEnvironmentRecordTests(unittest.TestCase):
    """The record has to reach the evidence, and reach it early enough."""

    def test_the_record_survives_the_build_manifest_round_trip(self):
        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})
        record = apply_semantic_environment(
            lock, environ={"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64}
        )
        manifest = BuildManifest.start(lock=lock, run_id="20240101-000000-abcdef")
        manifest.execution_environment = record

        restored_manifest = BuildManifest.from_dict(manifest.to_dict())

        self.assertEqual(restored_manifest.execution_environment, record)
        self.assertEqual(
            restored_manifest.to_dict()["execution_environment"]["cleared_ambient"],
            [DANGEROUS_AMBIENT],
        )

    def test_execute_plan_rebuilds_the_environment_before_the_first_child_process(self):
        # Run through the real image resolver to its process boundary. Stop
        # there so the test needs neither Docker nor prepared source packages.
        class ProcessBoundaryReached(Exception):
            pass

        lock = _lock_with_environment({"A8_PROOF_LEVEL": "native"})

        def inspect_image(argv):
            self.assertEqual(argv[:3], ["docker", "image", "inspect"])
            self.assertEqual(os.environ["A8_PROOF_LEVEL"], "native")
            self.assertNotIn(DANGEROUS_AMBIENT, os.environ)
            raise ProcessBoundaryReached

        docker_runner = mock.Mock(side_effect=inspect_image)
        build_runner = mock.Mock(side_effect=AssertionError("Unexpected build process"))
        suite_runner = mock.Mock(side_effect=AssertionError("Unexpected suite execution"))
        with tempfile.TemporaryDirectory(prefix="a8-test-e2e-env-order-") as tmp:
            root = Path(tmp)
            layout = RunnerLayout(root / "runner")
            for relative in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES:
                path = layout.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            from ops.a8.e2e.planner import EXTERNAL_TEST_DIRS
            layout.runner_version_file.parent.mkdir(parents=True, exist_ok=True)
            layout.runner_version_file.write_text("a8-runner/2.0.0\n", encoding="utf-8")
            for _, rel in EXTERNAL_TEST_DIRS:
                ext_dir = layout.root / rel
                ext_dir.mkdir(parents=True, exist_ok=True)
                (ext_dir / "placeholder.txt").write_text("ok\n", encoding="utf-8")
            request = ExecutionRequest(
                lock=lock,
                lock_path=root / "run.lock.json",
                package_dir=root / "package",
                output_dir=root / "out",
                workspace_dir=root / "work",
                run_id="environment-order-test",
            )
            with mock.patch.dict(
                os.environ,
                {"A8_PROOF_LEVEL": "smoke", DANGEROUS_AMBIENT: "0" * 64},
                clear=True,
            ):
                with self.assertRaises(ProcessBoundaryReached):
                    execute_plan(
                        request,
                        runner_root=layout.root,
                        docker_runner=docker_runner,
                        build_runner=build_runner,
                        suite_runner=suite_runner,
                    )
        docker_runner.assert_called_once()
        build_runner.assert_not_called()
        suite_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
