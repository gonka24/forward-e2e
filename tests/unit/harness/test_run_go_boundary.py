"""Path-safety and build-recipe tests for the offline Go boundary runner
(``scripts/run_go_boundary.py``).

``validate_paths`` is the guard that keeps the boundary evidence out of the
runner checkout, out of the external harness and out of the Gonka snapshot;
``generate_dockerfile`` is the recipe that compiles the runner-owned module
against a read-only Gonka. Both are exercised directly on synthetic
directories and strings; no Docker build is attempted.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_go_boundary.py"
SPEC = importlib.util.spec_from_file_location("run_go_boundary", SCRIPT)
assert SPEC and SPEC.loader
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


class ValidatePathsTests(unittest.TestCase):
    def test_accepts_separate_checkouts_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            runner = base / "runner"
            gonka = base / "gonka"
            harness = runner / "harness" / "go_boundary"
            output = base / "evidence"
            for path in (harness, gonka, output):
                path.mkdir(parents=True)
            with patch.object(boundary, "RUNNER_ROOT", runner):
                boundary.validate_paths(gonka, output, harness)

    def test_rejects_output_inside_runner_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            runner = base / "runner"
            gonka = base / "gonka"
            harness = runner / "harness"
            output = runner / "artifacts"
            with patch.object(boundary, "RUNNER_ROOT", runner):
                with self.assertRaisesRegex(boundary.BoundaryError, "runner checkout"):
                    boundary.validate_paths(gonka, output, harness)

    def test_rejects_output_overlapping_external_harness(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            runner = base / "runner"
            gonka = base / "gonka"
            harness = base / "external-harness"
            output = harness / "evidence"
            with patch.object(boundary, "RUNNER_ROOT", runner):
                with self.assertRaisesRegex(boundary.BoundaryError, "must not overlap"):
                    boundary.validate_paths(gonka, output, harness)

    def test_rejects_output_inside_gonka_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            runner = base / "runner"
            gonka = base / "gonka"
            harness = runner / "harness"
            output = gonka / "evidence"
            with patch.object(boundary, "RUNNER_ROOT", runner):
                with self.assertRaisesRegex(boundary.BoundaryError, "Gonka snapshot"):
                    boundary.validate_paths(gonka, output, harness)


class GeneratedBuildRecipeTests(unittest.TestCase):
    """The recipe must test the runner-owned module, never a package in Gonka.

    A drift of the module path or the test package would still "run" -- Go
    would compile whatever it found -- but the classification evidence would
    no longer describe the probes the catalog promises.
    """

    #: The smallest Gonka Dockerfile prefix the recipe accepts: it must name
    #: the pinned builder and contain the ``ARG LDFLAGS`` split marker.
    SYNTHETIC_GONKA_DOCKERFILE = (
        f"FROM {boundary.PINNED_BUILDER} AS builder\n"
        "WORKDIR /app\n"
        "ARG LDFLAGS\n"
        "RUN echo gonka-specific-build-steps-that-must-not-leak\n"
    )

    def test_go_mod_edit_flags_rename_the_module_to_the_runner_owned_boundary_path(self):
        flags = boundary.replace_flags({"go": "1.24", "toolchain": None, "replaces": []})
        self.assertIn("-module=github.com/gonka24/forward-e2e-go-boundary", flags)
        self.assertIn(f"-replace={boundary.INFERENCE_MODULE}=../inference-chain", flags)
        self.assertIn("-go=1.24", flags)

    def test_generated_go_test_argv_targets_the_query_faults_package_of_the_boundary_module(self):
        flags = boundary.replace_flags({"go": "1.24", "toolchain": None, "replaces": []})
        dockerfile = boundary.generate_dockerfile(self.SYNTHETIC_GONKA_DOCKERFILE, flags)
        # The Gonka prefix is kept verbatim up to the split marker and nothing
        # after it survives: the boundary stage replaces Gonka's own build.
        self.assertTrue(dockerfile.startswith(f"FROM {boundary.PINNED_BUILDER} AS builder\n"))
        self.assertNotIn("gonka-specific-build-steps-that-must-not-leak", dockerfile)
        go_test_lines = [line for line in dockerfile.splitlines() if "go test" in line]
        self.assertEqual(len(go_test_lines), 1, dockerfile)
        argv = go_test_lines[0].replace("\\", "").split()
        self.assertEqual(argv[:2], ["go", "test"])
        self.assertIn("-mod=mod", argv)
        self.assertIn("-json", argv)
        self.assertEqual(argv[-1], "./query_faults")
        self.assertIn("go mod edit '-module=github.com/gonka24/forward-e2e-go-boundary'", dockerfile)
        for required in boundary.REQUIRED_TESTS:
            self.assertIn(required, dockerfile)

    def test_a_gonka_dockerfile_without_the_pinned_builder_or_split_marker_is_refused(self):
        flags = boundary.replace_flags({"go": None, "toolchain": None, "replaces": []})
        for text in ("FROM golang:latest AS builder\nARG LDFLAGS\n",
                     f"FROM {boundary.PINNED_BUILDER} AS builder\nRUN true\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(boundary.BoundaryError, "pinned builder layout"):
                    boundary.generate_dockerfile(text, flags)


if __name__ == "__main__":
    unittest.main()
