"""Runner source identity is measured from the selected Git objects at build time.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.execution.errors import IntegrityError
from forward_e2e.execution.runner_source import bake_runner_source, read_runner_source


class RunnerSourceTests(unittest.TestCase):
    def test_build_records_the_verified_commit_and_tree_of_a_pristine_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("forward_e2e.execution.runner_source.subprocess.check_output",
                       side_effect=["a" * 40 + "\n", "", "b" * 40 + "\n"]):
                bake_runner_source(root, "a" * 40, "https://example.org/runner.git")
            self.assertEqual(read_runner_source(root), {
                "repo_url": "https://example.org/runner.git",
                "commit_sha": "a" * 40, "tree_sha": "b" * 40,
            })

    def test_build_refuses_a_different_commit_or_modified_checkout(self):
        for responses in (["b" * 40], ["a" * 40, " M scripts/acceptance_harness.py"]):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("forward_e2e.execution.runner_source.subprocess.check_output", side_effect=responses):
                    with self.assertRaises(IntegrityError):
                        bake_runner_source(root, "a" * 40, "https://example.org/runner.git")
                self.assertFalse((root / "runner-source.json").exists())

    def test_a_missing_or_invalid_baked_identity_cannot_create_a_runner_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(IntegrityError):
                read_runner_source(root)
            body = {"schema_version": 1, "repo_url": "https://example.org/runner.git",
                    "commit_sha": "a" * 40, "tree_sha": "b" * 40}
            for key, invalid in (("commit_sha", "HEAD"), ("tree_sha", "abc123"),
                                 ("repo_url", ""), ("schema_version", 99)):
                with self.subTest(key=key):
                    changed = {**body, key: invalid}
                    (root / "runner-source.json").write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(IntegrityError):
                        read_runner_source(root)
