"""Unit tests for Git worktree detection and git bundle handling in container runner.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from forward_e2e.suite.runtime import (
    SuiteRuntimeError,
    clone_from_bundle,
    create_git_bundle,
)


class WorktreeBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-bundle-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.repo = self.root / "fake_repo"
        self.bundle_output = self.root / "out" / "marketplace.bundle"

        self.repo.mkdir(parents=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_worktree_with_unreachable_gitdir_raises_actionable_error(self):
        # A git worktree has a file named .git pointing to common gitdir
        git_file = self.repo / ".git"
        git_file.write_text("gitdir: /c/Users/host/project/.git/worktrees/wt-branch\n")

        # Mock run_cmd to simulate rev-parse failing because the host path doesn't exist inside container
        def fake_run_cmd(cmd, check=True, **kwargs):
            if "rev-parse" in cmd:
                res = MagicMock()
                res.returncode = 128
                res.stdout = ""
                res.stderr = "fatal: not a git repository"
                if check:
                    raise subprocess.CalledProcessError(128, cmd, output=res.stdout, stderr=res.stderr)
                return res
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("forward_e2e.suite.runtime.run_cmd", side_effect=fake_run_cmd):
            with self.assertRaises(SuiteRuntimeError) as cm:
                create_git_bundle(self.repo, self.bundle_output)

            err = str(cm.exception)
            self.assertIn("appears to be a Git worktree", err)
            self.assertIn("Run-E2E.ps1", err)
            self.assertIn("run-e2e.sh", err)

    def test_standard_git_repo_creates_bundle(self):
        # Create normal .git directory
        (self.repo / ".git").mkdir()

        git_prefix = ["git", "-C", str(self.repo)]
        commands = [
            git_prefix + ["rev-parse", "HEAD"],
            git_prefix + ["status", "--porcelain"],
            git_prefix + ["bundle", "create", str(self.bundle_output), "HEAD"],
        ]

        def fake_run_cmd(cmd, **kwargs):
            self.assertIn(cmd, commands)
            stdout = ""
            if cmd == commands[0]:
                stdout = "beef00112233445566778899aabbccddeeff0011\n"
            elif cmd == commands[2]:
                # Honor the requested output only after validating argv, keeping
                # accidental writes outside the temporary fixture impossible.
                Path(cmd[-2]).write_bytes(b"NORMAL_BUNDLE_DATA")
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        with patch("forward_e2e.suite.runtime.run_cmd", side_effect=fake_run_cmd) as mock_run:
            sha = create_git_bundle(self.repo, self.bundle_output)
            self.assertEqual(sha, "beef00112233445566778899aabbccddeeff0011")
            self.assertEqual(self.bundle_output.read_bytes(), b"NORMAL_BUNDLE_DATA")
            self.assertEqual(mock_run.call_args_list, [call(cmd) for cmd in commands])

    def test_clone_from_bundle_validates_head_sha(self):
        bundle_file = self.root / "source.bundle"
        bundle_file.write_bytes(b"BUNDLE_DATA")
        target_dir = self.root / "target_clone"

        # Simulate git clone succeeding, but rev-parse returning mismatching sha
        def fake_run_cmd(cmd, check=True, **kwargs):
            if "clone" in cmd:
                target_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0)
            if "rev-parse" in cmd:
                return MagicMock(returncode=0, stdout="mismatch_sha_1234\n")
            return MagicMock(returncode=0)

        with patch("forward_e2e.suite.runtime.run_cmd", side_effect=fake_run_cmd):
            with self.assertRaises(SuiteRuntimeError) as cm:
                clone_from_bundle(bundle_file, target_dir, expected_head="expected_sha_5678")
            self.assertIn("does not match expected bundle HEAD", str(cm.exception))

    def test_clone_from_bundle_rejects_existing_target(self):
        bundle_file = self.root / "source.bundle"
        bundle_file.write_bytes(b"BUNDLE_DATA")
        target_dir = self.root / "existing_target"
        target_dir.mkdir()

        with self.assertRaises(SuiteRuntimeError) as cm:
            clone_from_bundle(bundle_file, target_dir, expected_head="any_sha")
        self.assertIn("already exists", str(cm.exception))

    def test_prepare_runtime_snapshot_with_use_direct_sources_points_directly_at_verified_checkouts_without_bundles(self):
        import json
        from forward_e2e.suite.runtime import prepare_runtime_snapshot

        mp_dir = (self.root / "marketplace_src").resolve()
        gonka_dir = (self.root / "gonka_src").resolve()
        runtime_root = (self.root / "runtime_root").resolve()
        (mp_dir / ".git").mkdir(parents=True)
        (gonka_dir / ".git").mkdir(parents=True)
        mp_sha = "1111111111111111111111111111111111111111"
        gonka_sha = "2222222222222222222222222222222222222222"
        tree_sha = "3333333333333333333333333333333333333333"

        calls = []

        def fake_run_cmd(cmd, check=True, **kwargs):
            calls.append(list(cmd))
            if "rev-parse" in cmd:
                if "HEAD^{tree}" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout=f"{tree_sha}\n", stderr="")
                repo_arg = Path(cmd[cmd.index("-C") + 1]).resolve()
                sha = mp_sha if repo_arg == mp_dir else gonka_sha
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{sha}\n", stderr="")
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected command in direct mode: {cmd}")

        with patch("forward_e2e.suite.runtime.run_cmd", side_effect=fake_run_cmd), \
             patch("forward_e2e.suite.runtime.check_native_filesystem"), \
             patch("forward_e2e.suite.runtime.perform_mount_probe"), \
             patch("forward_e2e.suite.runtime.verify_toolchain_versions", return_value={"cosmwasm-check": "2.2.2"}):
            snapshot = prepare_runtime_snapshot(
                marketplace_source=mp_dir,
                gonka_source=gonka_dir,
                run_id="direct-snapshot-01",
                runtime_root=runtime_root,
                fixed_marketplace_sha=mp_sha,
                fixed_gonka_sha=gonka_sha,
                use_direct_sources=True,
            )

        self.assertEqual(snapshot.marketplace_dir, mp_dir)
        self.assertEqual(snapshot.gonka_dir, gonka_dir)
        self.assertFalse(any("bundle" in c or "clone" in c for c in calls))
        identity = json.loads(snapshot.identity_file.read_text(encoding="utf-8"))
        self.assertEqual(identity["source_mode"], "direct_immutable")
        self.assertEqual(identity["marketplace_commit_sha"], mp_sha)
        self.assertEqual(identity["gonka_commit_sha"], gonka_sha)


if __name__ == "__main__":
    unittest.main()
