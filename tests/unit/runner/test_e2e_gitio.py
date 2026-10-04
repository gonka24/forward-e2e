"""Regression tests for Git diagnostics and credential isolation.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from forward_e2e.execution.errors import GitCommandError, SourceSpecError
from forward_e2e.execution.gitio import CredentialProvider, GitClient, REDACTED, redact, validate_remote_url


class GitCredentialIsolationTests(unittest.TestCase):
    def test_machine_output_preserves_carriage_returns_and_non_utf8_path_bytes(self):
        raw = b"path\rname\r\nwith-\xff\x00"
        runner = Mock(return_value=subprocess.CompletedProcess(["git"], 0, raw, b""))
        result = GitClient(runner=runner).run(["ls-tree", "-rz", "HEAD"], preserve_output=True)
        self.assertIs(runner.call_args.kwargs["text"], False)
        self.assertEqual(result.stdout.encode("utf-8", errors="surrogateescape"), raw)

    def test_binary_capture_errors_still_redact_credentials_in_diagnostics(self):
        runner = Mock(return_value=subprocess.CompletedProcess(
            ["git"], 128, b"", b"Authorization: Bearer synthetic-secret\r\nfatal: denied\xff",
        ))
        with self.assertRaises(GitCommandError) as caught:
            GitClient(runner=runner).run(["ls-tree", "-rz", "HEAD"], preserve_output=True)
        self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))
        self.assertIn(REDACTED, caught.exception.details["stderr"])

    def test_failed_commands_mask_credentials_without_relying_on_github_token_prefixes(self):
        for stderr, secret in (
            ("Authorization: Bearer synthetic-secret", "synthetic-secret"),
            ("Authorization: Basic c3ludGhldGlj", "c3ludGhldGlj"),
            ("x-access-token:synthetic-secret", "synthetic-secret"),
            ("https://user:synthetic-secret@example.test/repo", "synthetic-secret"),
            ("https://synthetic-secret@example.test/repo", "synthetic-secret"),
        ):
            with self.subTest(stderr=stderr):
                runner = Mock(return_value=subprocess.CompletedProcess(
                    ["git", "fetch"], 128, "", stderr,
                ))
                with self.assertRaises(GitCommandError) as caught:
                    GitClient(runner=runner).run(["fetch"])
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, str(caught.exception.to_dict()))
                self.assertIn(REDACTED, caught.exception.details["stderr"])

    def test_masking_an_authorization_header_preserves_the_following_diagnostic_line(self):
        self.assertEqual(
            redact("Authorization: Bearer synthetic-secret\r\nfatal: denied"),
            f"Authorization: {REDACTED}\r\nfatal: denied",
        )

    def test_whitespace_around_a_credential_url_never_echoes_the_credential_in_errors(self):
        url = "https://user:synthetic-secret@example.test/repo"
        for candidate in (" " + url, url + " ", "\t" + url + "\n"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SourceSpecError) as caught:
                    validate_remote_url(candidate)
                self.assertIn("whitespace", str(caught.exception))
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))

    def test_remote_url_rejects_query_or_fragment_credentials_without_echoing_them(self):
        """Repository URLs cannot carry bearer material outside userinfo.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        for suffix in ("?access_token=synthetic-secret", "#synthetic-secret"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(SourceSpecError) as caught:
                    validate_remote_url("https://example.test/repo.git" + suffix)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))
        for candidate in (
            "http://example.test/repo.git?token=synthetic-secret",
            "git@example.test:repo.git?token=synthetic-secret",
            "https://user:synthetic-secret@example.test/repo.git?token=secondary-secret",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SourceSpecError) as caught:
                    validate_remote_url(candidate)
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))
                self.assertNotIn("secondary-secret", str(caught.exception.to_dict()))

    def test_inherited_and_extra_configuration_cannot_restore_credentials_or_prompts(self):
        polluted = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "synthetic-helper",
            "GIT_CONFIG_KEY_1": "url.https://example.test/.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://another.example.test/",
            "GIT_CONFIG_PARAMETERS": "'credential.helper=synthetic-helper'",
            "GIT_CONFIG": "/synthetic/config",
            "GIT_CONFIG_SYSTEM": "/synthetic/system-config",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/synthetic/global-config",
            "GIT_ASKPASS": "/synthetic/askpass",
            "SSH_ASKPASS": "/synthetic/ssh-askpass",
            "E2E_GIT_USERNAME": "synthetic-user",
            "E2E_GIT_SECRET_FILE": "/synthetic/secret",
            "GIT_TERMINAL_PROMPT": "1",
            "GIT_LFS_SKIP_SMUDGE": "0",
            "GIT_DIR": "/synthetic/git",
            "GIT_WORK_TREE": "/synthetic/worktree",
            "GIT_INDEX_FILE": "/synthetic/index",
        }
        for via_extra in (False, True):
            for allow_credentials in (False, True):
                with self.subTest(via_extra=via_extra, allow_credentials=allow_credentials):
                    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
                    with patch.dict(os.environ, {} if via_extra else polluted, clear=True):
                        GitClient(runner=runner).run(
                            ["fetch"], env=polluted if via_extra else None,
                            allow_credentials=allow_credentials,
                        )
                    env = runner.call_args.kwargs["env"]
                    self.assertEqual(
                        {key: value for key, value in env.items() if key.startswith("GIT_CONFIG")},
                        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                         "GIT_CONFIG_VALUE_0": "", "GIT_CONFIG_GLOBAL": os.devnull},
                    )
                    self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                    self.assertEqual(env["GIT_LFS_SKIP_SMUDGE"], "1")
                    self.assertEqual(env["GIT_ASKPASS"], "/bin/false")
                    self.assertEqual(env["SSH_ASKPASS"], "/bin/false")
                    for key in ("E2E_GIT_USERNAME", "E2E_GIT_SECRET_FILE", "GIT_DIR",
                                "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                        self.assertNotIn(key, env)

    def test_inherited_system_config_cannot_rewrite_a_remote_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            hostile_config = Path(tmp) / "hostile.gitconfig"
            hostile_config.write_text(
                '[url "https://other.example/"]\n\tinsteadOf = https://github.com/\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": str(hostile_config)}):
                result = GitClient().run(["config", "--get-regexp", "^url\\."], check=False)
            self.assertNotIn("other.example", result.stdout or "")

    def test_local_commands_do_not_require_a_secret_file_or_create_a_helper_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for helper_dir in (None, root / "helper"):
                with self.subTest(helper_dir=helper_dir):
                    runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
                    client = GitClient(
                        credentials=CredentialProvider(root / "missing-secret"),
                        helper_dir=helper_dir, runner=runner,
                    )
                    client.run(["--version"])
                    runner.assert_called_once()
                    self.assertEqual(list(root.iterdir()), [])

    def test_remote_authentication_creates_the_helper_only_when_first_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "secret"
            secret.write_text("synthetic-secret", encoding="utf-8")
            helper_dir = root / "helper"
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
            client = GitClient(
                credentials=CredentialProvider(secret), helper_dir=helper_dir, runner=runner,
            )
            client.run(["--version"])
            self.assertFalse(helper_dir.exists())
            client.run(["fetch"], allow_credentials=True)
            env = runner.call_args.kwargs["env"]
            helper = Path(env["GIT_ASKPASS"])
            self.assertTrue(helper.is_file())
            self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["E2E_GIT_SECRET_FILE"], str(secret))
            self.assertEqual(env["E2E_GIT_USERNAME"], "x-access-token")
            self.assertNotIn("synthetic-secret", helper.read_text(encoding="utf-8"))
            client.run(["--version"])
            local_env = runner.call_args.kwargs["env"]
            self.assertEqual(local_env["GIT_ASKPASS"], "/bin/false")
            self.assertEqual(local_env["SSH_ASKPASS"], "/bin/false")
            self.assertNotIn("E2E_GIT_SECRET_FILE", local_env)

    def test_remote_authentication_still_rejects_a_missing_secret_before_invoking_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock()
            client = GitClient(
                credentials=CredentialProvider(Path(tmp) / "missing-secret"),
                helper_dir=Path(tmp) / "helper", runner=runner,
            )
            with self.assertRaises(SourceSpecError):
                client.run(["fetch"], allow_credentials=True)
            runner.assert_not_called()

    def test_explicit_commit_identity_survives_environment_hardening(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        identity = {"GIT_AUTHOR_NAME": "Synthetic Author", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00"}
        GitClient(runner=runner).run(["commit"], env=identity)
        env = runner.call_args.kwargs["env"]
        for key, value in identity.items():
            self.assertEqual(env[key], value)
