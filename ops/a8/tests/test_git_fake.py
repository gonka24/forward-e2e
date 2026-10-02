"""Guard the process fake against accepting broken Git invocations.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from pathlib import Path
import tempfile
import unittest

from ops.a8.tests.support.git import FakeGitRunner, PINNED_SHA


class GitFakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="a8-test-git-fake-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runner = FakeGitRunner()
        self.ref = "refs/e2e/source/gonka"

    def create_bundle(self, repository, filename, sha=PINNED_SHA):
        self.runner(["git", "-C", str(repository), "update-ref", self.ref, sha])
        bundle = self.root / filename
        self.assertEqual(self.runner([
            "git", "-C", str(repository), "bundle", "create", str(bundle), self.ref
        ]).returncode, 0)
        return bundle

    def test_a_misspelled_command_is_rejected_even_when_a_later_argument_is_a_known_command(self):
        bundle = self.create_bundle(self.root / "repo", "source.bundle")
        checkout = self.root / "checkout"
        self.assertEqual(self.runner(["git", "clone", str(bundle), str(checkout)]).returncode, 0)
        self.assertEqual(self.runner([
            "git", "-C", str(checkout), "checkout", "--detach", PINNED_SHA
        ]).returncode, 0)
        with self.assertRaises(AssertionError):
            self.runner(["git", "chekout", "--detach", PINNED_SHA])
        with self.assertRaises(AssertionError):
            self.runner(["git", "chekout", "fetch"])

    def test_global_option_values_are_not_mistaken_for_commands(self):
        self.runner.fetch_returncode = 128
        result = self.runner([
            "git", "-C", "clone", "-c", "safe.directory=fetch", "fetch", "source", PINNED_SHA
        ], cwd=self.root)
        self.assertEqual(result.returncode, 128)
        self.assertFalse((self.root / "clone").exists())
        repo, command, args = self.runner.parse_command(
            ["git", "-C", "clone", "-C", "nested", "fetch", "source"], cwd=self.root
        )
        self.assertEqual(repo, self.root / "clone" / "nested")
        self.assertEqual((command, args), ("fetch", ["source"]))

    def test_supported_clone_flags_accept_a_bundle_but_a_misspelled_flag_creates_no_checkout(self):
        bundle = self.create_bundle(self.root / "repo", "source.bundle")
        checkout = self.root / "checkout"
        argv = ["git", "clone", "--no-checkout", "--quiet", str(bundle), str(checkout)]
        invalid = list(argv)
        invalid[2] = "--no-chekout"
        with self.assertRaisesRegex(AssertionError, "Unsupported clone arguments"):
            self.runner(invalid)
        self.assertFalse(checkout.exists())
        self.assertEqual(self.runner(argv).returncode, 0)
        self.assertTrue((checkout / ".git").is_dir())

    def test_clone_rejects_extra_operands_and_options_in_place_of_paths(self):
        bundle = self.create_bundle(self.root / "repo", "source.bundle")
        checkout = self.root / "checkout"
        for args in (
            ["unexpected", str(bundle), str(checkout)],
            ["--no-checkout", str(bundle), "--unknown"],
            ["--no-chekout", str(checkout)],
        ):
            with self.subTest(args=args), self.assertRaises(AssertionError):
                self.runner(["git", "clone", *args])
            self.assertFalse(checkout.exists())

    def test_unsupported_or_incomplete_global_options_fail_explicitly(self):
        for argv in (["git", "-C"], ["git", "--unknown", "fetch"], ["git", "-c", "fetch"]):
            with self.subTest(argv=argv), self.assertRaises(AssertionError):
                self.runner(argv)

    def test_bundle_refs_are_snapshots_of_the_selected_repository_and_ref(self):
        first = self.create_bundle(self.root / "first-repo", "first.bundle")
        other_sha = "2" * 40
        second = self.create_bundle(self.root / "second-repo", "second.bundle", other_sha)
        # Changing a repository after export must not rewrite a bundle's identity.
        self.runner(["git", "-C", str(self.root / "first-repo"), "update-ref", self.ref, "3" * 40])
        self.assertEqual(self.runner(["git", "ls-remote", str(first)]).stdout.decode("utf-8"),
                         f"{PINNED_SHA}\t{self.ref}\n")
        self.assertEqual(self.runner(["git", "ls-remote", str(second)]).stdout.decode("utf-8"),
                         f"{other_sha}\t{self.ref}\n")

    def test_missing_changed_and_unregistered_bundles_are_not_valid_git_inputs(self):
        bundle = self.create_bundle(self.root / "repo", "source.bundle")
        self.assertEqual(self.runner(["git", "bundle", "verify", str(bundle)]).returncode, 0)
        original = bundle.read_bytes()
        for defect in ("missing", "changed", "unregistered"):
            with self.subTest(defect=defect):
                bundle.write_bytes(original)
                target = bundle
                if defect == "missing":
                    bundle.unlink()
                elif defect == "changed":
                    bundle.write_bytes(original + b"changed")
                else:
                    target = self.root / "unregistered.bundle"
                    target.write_bytes(original)
                self.assertNotEqual(self.runner(["git", "bundle", "verify", str(target)]).returncode, 0)
                self.assertNotEqual(self.runner(["git", "ls-remote", str(target)]).returncode, 0)
                checkout = self.root / "checkout"
                self.assertNotEqual(self.runner(["git", "clone", str(target), str(checkout)]).returncode, 0)
                self.assertFalse(checkout.exists())

    def test_unknown_bundle_operations_do_not_fall_through_to_success(self):
        bundle = self.create_bundle(self.root / "repo", "source.bundle")
        with self.assertRaises(AssertionError):
            self.runner(["git", "bundle", "verfy", str(bundle)])

    def test_an_existing_fixture_bundle_can_be_registered_for_replay(self):
        bundle = self.root / "replay.bundle"
        bundle.write_bytes(b"synthetic recorded bundle")
        self.runner.register_bundle(bundle, {self.ref: PINNED_SHA})
        self.assertEqual(self.runner(["git", "bundle", "verify", str(bundle)]).returncode, 0)
        self.assertEqual(self.runner(["git", "ls-remote", str(bundle)]).stdout.decode("utf-8"),
                         f"{PINNED_SHA}\t{self.ref}\n")

    def test_each_cloned_repository_accepts_only_its_bundle_commit_and_reports_its_own_head(self):
        first = self.create_bundle(self.root / "first", "first.bundle")
        other_sha = "2" * 40
        second = self.create_bundle(self.root / "second", "second.bundle", other_sha)
        for bundle, sha, foreign in ((first, PINNED_SHA, other_sha), (second, other_sha, PINNED_SHA)):
            checkout = self.root / (bundle.stem + "-checkout")
            self.assertEqual(self.runner(["git", "clone", str(bundle), str(checkout)]).returncode, 0)
            prefix = ["git", "-C", str(checkout)]
            self.assertNotEqual(self.runner(prefix + ["rev-parse", "HEAD"]).returncode, 0)
            self.assertEqual(self.runner(prefix + ["checkout", "--detach", sha]).returncode, 0)
            self.assertNotEqual(self.runner(prefix + ["checkout", "--detach", foreign]).returncode, 0)
            self.assertEqual(self.runner(prefix + ["rev-parse", "HEAD"]).stdout.decode("utf-8"), sha + "\n")
        self.assertEqual(self.runner([
            "git", "-C", str(self.root / "first-checkout"), "rev-parse", "HEAD"
        ]).stdout.decode("utf-8"), PINNED_SHA + "\n")

    def test_rev_parse_uses_only_its_arguments_even_when_a_directory_is_named_head(self):
        runner = FakeGitRunner(head_sha="2" * 40)
        self.assertEqual(runner([
            "git", "-C", "HEAD", "rev-parse", PINNED_SHA + "^{commit}"
        ], cwd=self.root).stdout.decode("utf-8"), PINNED_SHA + "\n")
        self.assertEqual(runner([
            "git", "-C", "HEAD", "rev-parse", "--git-dir"
        ], cwd=self.root).stdout.decode("utf-8"), ".git\n")
        with self.assertRaises(AssertionError):
            runner(["git", "-C", "HEAD", "rev-parse", "--unsupported"], cwd=self.root)
