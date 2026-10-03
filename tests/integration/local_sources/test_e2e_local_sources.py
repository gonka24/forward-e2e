"""Local Git integration tests, deliberately outside offline test discovery.

These tests require host-installed Git and create throwaway local repositories.
All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.execution.gitio import GitClient
from forward_e2e.execution.sources import SourceAcquirer, build_source_spec, sha256_path


@unittest.skipUnless(shutil.which("git"), "git not available")
class E2ELocalDirtyWorktreeTests(unittest.TestCase):
    """Only committed objects travel: the user's files are never touched."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.addCleanup(self.tmp_dir.cleanup)
        self.root = Path(self.tmp_dir.name).resolve()
        self.repo = self.root / "user-repo"
        self.repo.mkdir()
        templates = self.root / "empty-templates"
        templates.mkdir()
        empty_config = self.root / "empty.gitconfig"
        empty_config.write_text("", encoding="utf-8")
        # Isolate fixture creation AND acquisition: inherited Git overrides can
        # redirect index/object writes or change the repository's object format.
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(
            {
                # An empty config file is portable; Windows Git treats the
                # `nul` device path differently from Git on Linux.
                "GIT_CONFIG_GLOBAL": str(empty_config),
                "GIT_CONFIG_SYSTEM": str(empty_config),
                "GIT_TEMPLATE_DIR": str(templates),
                "GIT_DEFAULT_HASH": "sha1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "A8 Test",
                "GIT_AUTHOR_EMAIL": "a8-test@example.invalid",
                "GIT_COMMITTER_NAME": "A8 Test",
                "GIT_COMMITTER_EMAIL": "a8-test@example.invalid",
            }
        )
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def _git(self, *args):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f"git fixture command failed: git {' '.join(args)}: {proc.stderr.strip()}")
        # Preserve leading status columns: " M" and "M " have different meanings.
        return proc.stdout.rstrip("\r\n")

    def test_an_unexpected_git_fixture_failure_fails_instead_of_skipping(self):
        failure = subprocess.CompletedProcess(["git", "init"], 1, "", "synthetic failure")
        with patch.object(subprocess, "run", return_value=failure):
            with self.assertRaises(AssertionError) as caught:
                self._git("init", "--quiet")
        self.assertIn("synthetic failure", str(caught.exception))

    def test_gitmodules_quoting_and_escapes_are_decoded_without_loading_included_files(self):
        self._git("init", "--quiet", "--object-format=sha1")
        included = self.root / "host-config"
        included.write_text('[submodule "foreign"]\npath = foreign\nurl = https://example.invalid/foreign\n')
        (self.repo / ".gitmodules").write_text(
            '[submodule "lib.with.dots"]\n'
            'path = "vendor/lib\\tquoted"\n'
            'url = "https://example.invalid/lib%20name.git" # trailing comment\n'
            f'[include]\npath = "{included.as_posix()}"\n',
            encoding="utf-8",
        )
        acquirer = SourceAcquirer(GitClient(), package_dir=self.root / "package",
                                  scratch_dir=self.root / "scratch")
        self.assertEqual(acquirer._read_gitmodules(self.repo),
                         {"vendor/lib\tquoted": "https://example.invalid/lib%20name.git"})
        # Ensure gitlink listing uses the same decoded path rather than Git's
        # display quoting, which escapes tabs and non-ASCII characters.
        if os.name != "nt":
            # Git for Windows rejects control characters in index paths. Linux
            # still exercises matching the decoded .gitmodules path to a gitlink.
            self._git("add", ".gitmodules")
            self._git("commit", "--quiet", "-m", "base")
            sha = self._git("rev-parse", "HEAD")
            self._git("update-index", "--add", "--cacheinfo", "160000", sha,
                      "vendor/lib\tquoted")
            self._git("commit", "--quiet", "-m", "gitlink")
            self.assertEqual(acquirer._gitlinks(self.repo), [("vendor/lib\tquoted", sha)])

    @unittest.skipIf(os.name == "nt", "Git for Windows rejects CR/LF in index paths")
    def test_carriage_returns_in_git_paths_survive_configuration_and_gitlink_decoding(self):
        self._git("init", "--quiet", "--object-format=sha1")
        paths = ["vendor/lib\rquoted", "vendor/lib\r\nquoted"]
        for index, path in enumerate(paths):
            self._git("config", "--file", ".gitmodules", f"submodule.lib{index}.path", path)
            self._git("config", "--file", ".gitmodules", f"submodule.lib{index}.url",
                      "https://example.invalid/lib.git")
        self._git("add", ".gitmodules")
        self._git("commit", "--quiet", "-m", "base")
        sha = self._git("rev-parse", "HEAD")
        for path in paths:
            self._git("update-index", "--add", "--cacheinfo", "160000", sha, path)
        self._git("commit", "--quiet", "-m", "gitlinks")
        acquirer = SourceAcquirer(GitClient(), package_dir=self.root / "package",
                                  scratch_dir=self.root / "scratch")
        self.assertEqual(acquirer._read_gitmodules(self.repo),
                         {path: "https://example.invalid/lib.git" for path in paths})
        self.assertEqual(sorted(acquirer._gitlinks(self.repo)),
                         sorted((path, sha) for path in paths))

    def test_a_dirty_checkout_at_another_head_still_yields_exactly_the_pinned_commit(self):
        self._git("init", "--quiet", "--object-format=sha1")
        (self.repo / "file.txt").write_text("first revision\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "--quiet", "-m", "first")
        first_sha = self._git("rev-parse", "HEAD")

        (self.repo / "file.txt").write_text("second revision\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "--quiet", "-m", "second")
        head_sha = self._git("rev-parse", "HEAD")
        self.assertNotEqual(first_sha, head_sha)

        # The user's checkout is left at the *second* commit, is dirty and has
        # an untracked file. Both must stay unchanged here and must not enter
        # the acquired snapshot.
        (self.repo / "file.txt").write_text("uncommitted local edits\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("scratch notes\n", encoding="utf-8")

        status_before = self._git("status", "--porcelain=v1", "--untracked-files=all")
        index_before = self._git("ls-files", "--stage")

        spec = build_source_spec(
            role="gonka",
            repo=None,
            path=str(self.repo),
            sha=first_sha,
            repo_flag="--gonka-repo",
            path_flag="--gonka-path",
            sha_flag="--gonka-sha",
        )
        acquirer = SourceAcquirer(
            GitClient(),
            package_dir=self.root / "package",
            scratch_dir=self.root / "scratch",
        )

        acquired = acquirer.acquire(spec, worktree_root=self.root / "src")

        # The snapshot is the requested commit, not HEAD and not the dirty tree.
        self.assertEqual(acquired.spec.commit_sha, first_sha)
        self.assertEqual((acquired.worktree / "file.txt").read_text(encoding="utf-8"), "first revision\n")
        self.assertFalse((acquired.worktree / "untracked.txt").exists())
        self.assertTrue(acquired.bundle_path.is_file())
        self.assertEqual(acquired.bundle_sha256, sha256_path(acquired.bundle_path))
        # No origin is configured, so no commit link is invented.
        self.assertIsNone(acquired.resolved_origin_url)
        self.assertIsNone(acquired.commit_url)

        # Acquisition must preserve files, HEAD, staging state and index entries.
        self.assertEqual(
            (self.repo / "file.txt").read_text(encoding="utf-8"), "uncommitted local edits\n"
        )
        self.assertEqual(
            (self.repo / "untracked.txt").read_text(encoding="utf-8"), "scratch notes\n"
        )
        self.assertEqual(self._git("rev-parse", "HEAD"), head_sha)
        self.assertEqual(
            self._git("status", "--porcelain=v1", "--untracked-files=all"), status_before
        )
        self.assertEqual(self._git("ls-files", "--stage"), index_before)


if __name__ == "__main__":
    unittest.main()
