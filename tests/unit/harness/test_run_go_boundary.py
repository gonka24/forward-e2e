"""Path-safety tests for the offline Go boundary runner."""

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


if __name__ == "__main__":
    unittest.main()
