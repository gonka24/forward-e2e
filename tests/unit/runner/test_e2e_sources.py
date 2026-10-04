"""Unit tests for ``forward_e2e.execution.sources``: strict SHA pinning, path safety,
bundle integrity, submodule/LFS policy and credential hygiene.

Git process calls are replaced with an injected fake runner. Tests requiring a
real local Git installation live separately in tests/integration/local_sources/.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from typing import get_type_hints

from forward_e2e.execution.errors import (
    GitCommandError,
    IntegrityError,
    PathSafetyError,
    SourceAcquisitionError,
    SourceSpecError,
    UnsupportedSourceError,
)
from forward_e2e.execution.gitio import (
    REDACTED,
    CredentialProvider,
    GitClient,
    redact,
    redact_argv,
    validate_remote_url,
)
from forward_e2e.execution.sources import (
    SourceAcquirer,
    SourceKind,
    assert_no_symlinks_in_path,
    github_commit_url,
    is_full_sha,
    build_source_spec,
    bundle_ref,
    normalize_full_sha,
    resolve_within,
    sha256_path,
)

from tests.unit.runner.support.git import FakeGitRunner, PINNED_SHA

OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"
SUBMODULE_SHA = "1234567890abcdef1234567890abcdef12345678"

GONKA_REPO_URL = "https://github.com/acme/widgets.git"


class E2ESourceSpecTests(unittest.TestCase):
    """Argument level contract: only a full commit SHA is ever accepted."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.root = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_full_forty_character_sha_is_accepted_in_either_case_and_canonicalised(self):
        self.assertEqual(normalize_full_sha(PINNED_SHA, flag="--gonka-sha"), PINNED_SHA)
        self.assertEqual(normalize_full_sha(PINNED_SHA.upper(), flag="--gonka-sha"), PINNED_SHA)

    def test_whitespace_around_a_sha_is_rejected_by_validation_and_commit_links(self):
        for sha in (" " + PINNED_SHA, PINNED_SHA + "\n"):
            self.assertFalse(is_full_sha(sha))
            self.assertIsNone(github_commit_url(GONKA_REPO_URL, sha))
            with self.assertRaises(SourceSpecError):
                normalize_full_sha(sha, flag="sha")

    def test_the_path_guard_annotations_can_be_resolved_without_missing_names(self):
        self.assertEqual(get_type_hints(assert_no_symlinks_in_path)["path"], str | Path)

    def test_a_branch_name_is_refused_with_an_explanation_instead_of_being_resolved(self):
        with self.assertRaises(SourceSpecError) as cm:
            normalize_full_sha("main", flag="--gonka-sha")
        self.assertIn("does not accept the symbolic revision", str(cm.exception))
        self.assertIn("--gonka-sha", str(cm.exception))
        self.assertEqual(cm.exception.exit_code, 2)

    def test_the_literal_head_is_refused_rather_than_silently_resolved(self):
        with self.assertRaises(SourceSpecError) as cm:
            normalize_full_sha("HEAD", flag="--contracts-sha")
        self.assertIn("does not accept the symbolic revision", str(cm.exception))

    def test_a_tag_name_is_refused_because_it_is_not_a_forty_character_sha(self):
        with self.assertRaises(SourceSpecError) as cm:
            normalize_full_sha("release-2024", flag="--gonka-sha")
        self.assertIn("must be exactly 40 hexadecimal characters", str(cm.exception))

    def test_an_abbreviated_sha_is_refused_as_too_short(self):
        with self.assertRaises(SourceSpecError) as cm:
            normalize_full_sha(PINNED_SHA[:12], flag="--gonka-sha")
        self.assertIn("abbreviated SHAs are refused", str(cm.exception))

    def test_a_fully_qualified_ref_name_is_refused(self):
        with self.assertRaises(SourceSpecError) as cm:
            normalize_full_sha("refs/heads/main", flag="--gonka-sha")
        self.assertIn("does not accept a ref name", str(cm.exception))

    def test_revision_expressions_are_refused_instead_of_being_evaluated(self):
        for expression in ("HEAD~1", "main@{upstream}", "v1.0^{commit}", f"{PINNED_SHA}~1"):
            with self.subTest(expression=expression):
                with self.assertRaises(SourceSpecError) as cm:
                    normalize_full_sha(expression, flag="--gonka-sha")
                self.assertIn("does not accept revision expressions", str(cm.exception))

    def test_supplying_both_a_repo_and_a_path_for_one_role_is_refused(self):
        with self.assertRaises(SourceSpecError) as cm:
            build_source_spec(
                role="gonka",
                repo=GONKA_REPO_URL,
                path=str(self.root),
                sha=PINNED_SHA,
                repo_flag="--gonka-repo",
                path_flag="--gonka-path",
                sha_flag="--gonka-sha",
            )
        self.assertIn("are mutually exclusive", str(cm.exception))
        self.assertIn("--gonka-repo", str(cm.exception))
        self.assertIn("--gonka-path", str(cm.exception))

    def test_supplying_neither_a_repo_nor_a_path_for_one_role_is_refused(self):
        with self.assertRaises(SourceSpecError) as cm:
            build_source_spec(
                role="contracts",
                repo=None,
                path="   ",
                sha=PINNED_SHA,
                repo_flag="--contracts-repo",
                path_flag="--contracts-path",
                sha_flag="--contracts-sha",
            )
        self.assertIn("source is missing", str(cm.exception))
        self.assertIn("--contracts-repo", str(cm.exception))

    def test_exactly_one_source_produces_a_typed_spec_with_a_lowercased_sha(self):
        remote = build_source_spec(
            role="gonka",
            repo=GONKA_REPO_URL,
            path=None,
            sha=PINNED_SHA.upper(),
            repo_flag="--gonka-repo",
            path_flag="--gonka-path",
            sha_flag="--gonka-sha",
        )
        self.assertIs(remote.kind, SourceKind.REMOTE)
        self.assertEqual(remote.repo_url, GONKA_REPO_URL)
        self.assertIsNone(remote.local_path)
        self.assertEqual(remote.commit_sha, PINNED_SHA)

        local = build_source_spec(
            role="contracts",
            repo=None,
            path=str(self.root),
            sha=PINNED_SHA,
            repo_flag="--contracts-repo",
            path_flag="--contracts-path",
            sha_flag="--contracts-sha",
        )
        self.assertIs(local.kind, SourceKind.LOCAL)
        self.assertEqual(local.local_path, str(self.root))
        self.assertIsNone(local.repo_url)


class E2ESourceAcquisitionTests(unittest.TestCase):
    """Acquisition behaviour driven through an injected fake process runner."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.package_dir = self.root / "package"
        self.scratch_dir = self.root / "scratch"
        self.worktree_root = self.root / "src"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _spec(self):
        return build_source_spec(
            role="gonka",
            repo=GONKA_REPO_URL,
            path=None,
            sha=PINNED_SHA,
            repo_flag="--gonka-repo",
            path_flag="--gonka-path",
            sha_flag="--gonka-sha",
        )

    def _acquirer(self, runner):
        return SourceAcquirer(
            GitClient(runner=runner),
            package_dir=self.package_dir,
            scratch_dir=self.scratch_dir,
        )

    def test_a_sha_that_names_a_tree_object_never_becomes_the_pinned_source(self):
        runner = FakeGitRunner(object_types=("tree",))
        acquirer = self._acquirer(runner)

        with self.assertRaises(SourceAcquisitionError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("does not contain the requested commit", str(cm.exception))
        self.assertIn("No other revision is substituted", str(cm.exception))
        self.assertFalse((self.package_dir / "bundles" / "gonka.bundle").exists())
        self.assertFalse((self.worktree_root / "gonka").exists())

    def test_a_sha_that_names_a_tag_object_is_rejected_at_the_commit_type_gate(self):
        # The object is reachable (the reachability probe sees a commit) but
        # the dedicated type assertion is what decides: it reports a tag, and
        # acquisition must stop there rather than snapshot it.
        runner = FakeGitRunner(object_types=("commit", "tag"))
        acquirer = self._acquirer(runner)

        with self.assertRaises(SourceSpecError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("is a tag object, not a commit", str(cm.exception))
        self.assertIn("an annotated tag object is not accepted", str(cm.exception))
        self.assertFalse((self.package_dir / "bundles" / "gonka.bundle").exists())

    def test_a_checkout_landing_on_another_commit_than_the_pinned_one_is_rejected(self):
        # Everything succeeds until the materialised worktree reports a HEAD
        # that is not the pinned SHA; the snapshot must not be accepted.
        runner = FakeGitRunner(head_sha=OTHER_SHA)
        acquirer = self._acquirer(runner)

        with self.assertRaises(IntegrityError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("Checked out HEAD does not match the pinned commit", str(cm.exception))

    def test_a_pinned_commit_is_bundled_verified_and_linked_to_its_github_commit(self):
        runner = FakeGitRunner(clone_contents={"README.md": "hello\n"})
        acquirer = self._acquirer(runner)

        acquired = acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertEqual(acquired.spec.commit_sha, PINNED_SHA)
        self.assertEqual(acquired.bundle_relpath, "bundles/gonka.bundle")
        self.assertEqual(acquired.bundle_ref, bundle_ref("gonka"))
        self.assertEqual(acquired.bundle_ref, "refs/e2e/source/gonka")
        self.assertTrue(acquired.bundle_path.is_file())
        self.assertEqual(acquired.bundle_sha256, hashlib.sha256(runner.bundle_bytes).hexdigest())
        self.assertEqual(acquired.resolved_origin_url, GONKA_REPO_URL)
        self.assertEqual(
            acquired.commit_url,
            f"https://github.com/acme/widgets/commit/{PINNED_SHA}",
        )
        self.assertEqual(acquired.submodules, [])
        self.assertEqual(acquired.fetch_strategy, "object")
        verify_calls = [
            call for call in runner.calls
            if "bundle" in call and "verify" in call
        ]
        self.assertEqual(len(verify_calls), 1)
        self.assertIn("-C", verify_calls[0])
        self.assertIn("bundle-verifier.git", " ".join(verify_calls[0]))

        record = acquired.to_lock_record()
        self.assertEqual(record["commit_sha"], PINNED_SHA)
        self.assertEqual(record["bundle_sha256"], acquired.bundle_sha256)

    def test_a_source_whose_content_lives_in_git_lfs_is_refused_as_unsupported(self):
        runner = FakeGitRunner(
            clone_contents={".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"}
        )
        acquirer = self._acquirer(runner)

        with self.assertRaises(UnsupportedSourceError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("uses Git LFS", str(cm.exception))
        self.assertIn("This source is unsupported", str(cm.exception))

    def test_a_relative_submodule_url_is_refused_by_the_supported_source_policy(self):
        gitmodules = '[submodule "vendor/lib"]\npath = vendor/lib\nurl = ../lib.git\n'
        runner = FakeGitRunner(
            clone_contents={".gitmodules": gitmodules},
            config_output="submodule.vendor/lib.path\nvendor/lib\x00"
                          "submodule.vendor/lib.url\n" + gitmodules.split("url = ")[1].strip() + "\x00",
            ls_tree_output=f"160000 commit {SUBMODULE_SHA}\tvendor/lib\x00",
        )
        acquirer = self._acquirer(runner)

        with self.assertRaises(UnsupportedSourceError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("Relative submodule URLs are not supported", str(cm.exception))

    def test_an_ssh_submodule_url_is_refused_as_an_unsupported_transport(self):
        gitmodules = (
            '[submodule "vendor/lib"]\npath = vendor/lib\nurl = git@github.com:acme/lib.git\n'
        )
        runner = FakeGitRunner(
            clone_contents={".gitmodules": gitmodules},
            config_output="submodule.vendor/lib.path\nvendor/lib\x00"
                          "submodule.vendor/lib.url\n" + gitmodules.split("url = ")[1].strip() + "\x00",
            ls_tree_output=f"160000 commit {SUBMODULE_SHA}\tvendor/lib\x00",
        )
        acquirer = self._acquirer(runner)

        with self.assertRaises(UnsupportedSourceError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("unsupported transport", str(cm.exception))
        self.assertIn("only https:// submodules", str(cm.exception))

    def test_rejected_submodule_url_does_not_echo_query_credentials(self):
        """Invalid .gitmodules URLs cannot leak token text into diagnostics.

        All fixtures are synthetic. No network, Docker, or live chain calls.
        """
        url = "https://example.test/lib.git?token=synthetic-secret"
        gitmodules = f'[submodule "vendor/lib"]\npath = vendor/lib\nurl = {url}\n'
        runner = FakeGitRunner(
            clone_contents={".gitmodules": gitmodules},
            config_output="submodule.vendor/lib.path\nvendor/lib\x00"
                          f"submodule.vendor/lib.url\n{url}\x00",
            ls_tree_output=f"160000 commit {SUBMODULE_SHA}\tvendor/lib\x00",
        )
        acquirer = self._acquirer(runner)

        with self.assertRaises(UnsupportedSourceError) as caught:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertNotIn("synthetic-secret", str(caught.exception.to_dict()))

    def test_a_submodule_gitlink_without_a_declared_url_is_refused(self):
        runner = FakeGitRunner(ls_tree_output=f"160000 commit {SUBMODULE_SHA}\tvendor/lib\x00")
        acquirer = self._acquirer(runner)

        with self.assertRaises(UnsupportedSourceError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("has no URL in .gitmodules", str(cm.exception))

    def _nested_snapshots(self, *, parent_path="vendor/lib", child_path="nested lib", extra_level=False):
        # Git's `config --null --list` emits key LF value NUL; `ls-tree -rz`
        # emits unquoted paths terminated by NUL. These are process-boundary
        # fixtures, not an independent parser for Git configuration syntax.
        def parent(path, sha):
            url = "https://github.com/acme/lib.git"
            return {
                "contents": {".gitmodules":
                    f'[submodule "lib"]\npath = "{path}"\nurl = "{url}"\n'},
                "config_output": f"submodule.lib.path\n{path}\x00submodule.lib.url\n{url}\x00",
                "ls_tree_output": f"160000 commit {sha}\t{path}\x00",
            }
        snapshots = {
            PINNED_SHA: parent(parent_path, SUBMODULE_SHA),
            SUBMODULE_SHA: parent(child_path, OTHER_SHA),
            OTHER_SHA: {"contents": {"README.md": "leaf source\n"}},
        }
        if extra_level:
            fourth_sha = "fedcba9876543210fedcba9876543210fedcba98"
            snapshots[OTHER_SHA] = parent("deep", fourth_sha)
            snapshots[fourth_sha] = {"contents": {"README.md": "deep source\n"}}
        return snapshots

    def test_nested_submodules_are_bundled_with_root_relative_paths_and_replayed_offline(self):
        from forward_e2e.execution.executor import _restore_submodules

        runner = FakeGitRunner(snapshots=self._nested_snapshots())
        acquirer = self._acquirer(runner)
        acquired = acquirer.acquire(self._spec(), worktree_root=self.worktree_root)
        self.assertEqual([s.path for s in acquired.submodules],
                         ["vendor/lib", "vendor/lib/nested lib"])
        self.assertEqual([s.commit_sha for s in acquired.submodules],
                         [SUBMODULE_SHA, OTHER_SHA])
        for sub in acquired.submodules:
            self.assertEqual(sha256_path(self.package_dir / sub.bundle_relpath), sub.bundle_sha256)
        replay = self.root / "replay"
        acquirer.materialise(acquired.bundle_path, replay, acquired.bundle_ref, PINNED_SHA)
        call_count = len(runner.calls)
        _restore_submodules(acquired.to_lock_record(), acquirer=acquirer,
                            package_dir=self.package_dir, worktree=replay)
        self.assertEqual((replay / "vendor/lib/nested lib/README.md").read_text(), "leaf source\n")
        self.assertFalse(any(runner.subcommand(c) == "fetch" for c in runner.calls[call_count:]))
        self.assertEqual(runner.repository_heads[replay / "vendor/lib"], SUBMODULE_SHA)
        self.assertEqual(runner.repository_heads[replay / "vendor/lib/nested lib"], OTHER_SHA)

    def test_submodule_paths_with_surrounding_spaces_are_rejected_before_fetching_the_child(self):
        for field, original in (("parent_path", "vendor/lib"), ("child_path", "nested lib")):
            for path in (" " + original, original + " "):
                with self.subTest(field=field, path=path), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    snapshots = self._nested_snapshots(**{field: path})
                    runner = FakeGitRunner(snapshots=snapshots)
                    acquirer = SourceAcquirer(GitClient(runner=runner),
                                              package_dir=root / "pkg", scratch_dir=root / "scratch")
                    with self.assertRaisesRegex(UnsupportedSourceError, "path normalisation"):
                        acquirer.acquire(self._spec(), worktree_root=root / "src")
                    child_sha = SUBMODULE_SHA if field == "parent_path" else OTHER_SHA
                    self.assertFalse(any(runner.subcommand(c) == "fetch" and child_sha in c
                                         for c in runner.calls))
                    parent = root / "src/gonka"
                    if field == "child_path":
                        parent /= "vendor/lib"
                    self.assertFalse((parent / path.strip()).exists())

    def test_non_utf8_submodule_paths_are_rejected_before_fetch_with_serialisable_diagnostics(self):
        from forward_e2e.execution.runlock import canonical_json

        for field in ("parent_path", "child_path"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                path = b"vendor/lib-\xff".decode("utf-8", errors="surrogateescape")
                snapshots = self._nested_snapshots(**{field: path})
                runner = FakeGitRunner(snapshots=snapshots)
                acquirer = SourceAcquirer(GitClient(runner=runner),
                                          package_dir=root / "pkg", scratch_dir=root / "scratch")
                with self.assertRaisesRegex(UnsupportedSourceError, "not valid UTF-8") as caught:
                    acquirer.acquire(self._spec(), worktree_root=root / "src")
                child_sha = SUBMODULE_SHA if field == "parent_path" else OTHER_SHA
                self.assertFalse(any(runner.subcommand(c) == "fetch" and child_sha in c
                                     for c in runner.calls))
                # Even error reporting must not leak surrogate code points into
                # UTF-8 output and turn a structured refusal into another crash.
                self.assertIn("\\udcff", caught.exception.details["submodule"])
                canonical_json(caught.exception.to_dict()).encode("utf-8")

    def test_three_distinct_submodule_commits_are_captured_when_the_depth_limit_allows_them(self):
        runner = FakeGitRunner(snapshots=self._nested_snapshots(extra_level=True))
        acquirer = SourceAcquirer(GitClient(runner=runner), package_dir=self.package_dir,
                                  scratch_dir=self.scratch_dir, max_submodule_depth=3)
        acquired = acquirer.acquire(self._spec(), worktree_root=self.worktree_root)
        self.assertEqual([sub.path for sub in acquired.submodules],
                         ["vendor/lib", "vendor/lib/nested lib", "vendor/lib/nested lib/deep"])
        self.assertEqual(len({sub.commit_sha for sub in acquired.submodules}), 3)
        self.assertEqual((acquired.worktree / "vendor/lib/nested lib/deep/README.md").read_text(),
                         "deep source\n")

    def test_a_nested_gitlink_beyond_the_supported_depth_is_rejected_instead_of_omitted(self):
        snapshots = self._nested_snapshots(extra_level=True)
        acquirer = self._acquirer(FakeGitRunner(snapshots=snapshots))
        with self.assertRaisesRegex(UnsupportedSourceError, "nesting exceeds"):
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

    def test_lfs_in_a_child_checkout_is_rejected_before_accepting_the_source(self):
        for sha in (SUBMODULE_SHA, OTHER_SHA):
            with self.subTest(sha=sha), tempfile.TemporaryDirectory() as tmp:
                snapshots = self._nested_snapshots()
                snapshots[sha]["contents"][".gitattributes"] = "*.bin filter=lfs diff=lfs merge=lfs -text\n"
                root = Path(tmp).resolve()
                acquirer = SourceAcquirer(GitClient(runner=FakeGitRunner(snapshots=snapshots)),
                                          package_dir=root / "pkg", scratch_dir=root / "scratch")
                with self.assertRaisesRegex(UnsupportedSourceError, "uses Git LFS"):
                    acquirer.acquire(self._spec(), worktree_root=root / "src")

    def test_invalid_git_configuration_is_reported_as_an_unsupported_source(self):
        snapshots = self._nested_snapshots()
        snapshots[PINNED_SHA]["config_returncode"] = 128
        acquirer = self._acquirer(FakeGitRunner(snapshots=snapshots))
        with self.assertRaisesRegex(UnsupportedSourceError, "cannot be parsed"):
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

    def test_a_scratch_object_store_is_never_reused_between_acquisitions(self):
        (self.scratch_dir / "gonka-objects").mkdir(parents=True)
        acquirer = self._acquirer(FakeGitRunner())

        with self.assertRaises(SourceAcquisitionError) as cm:
            acquirer.acquire(self._spec(), worktree_root=self.worktree_root)

        self.assertIn("refusing to reuse it", str(cm.exception))

    def test_acquire_without_bundle_clones_directly_without_writing_git_bundles(self):
        runner = FakeGitRunner(snapshots=self._nested_snapshots())
        acquirer = self._acquirer(runner)
        acquired = acquirer.acquire(
            self._spec(), worktree_root=self.worktree_root, create_bundle=False
        )
        self.assertEqual(acquired.spec.commit_sha, PINNED_SHA)
        self.assertEqual(acquired.bundle_sha256, "")
        self.assertEqual(acquired.to_lock_record()["bundle_sha256"], "")
        self.assertEqual(acquired.bundle_path, self.scratch_dir / "gonka-objects")
        self.assertFalse((self.package_dir / "bundles" / "gonka.bundle").exists())
        self.assertFalse(any(runner.subcommand(c) == "bundle" for c in runner.calls))
        self.assertTrue((acquired.worktree / "vendor/lib/nested lib/README.md").is_file())
        for sub in acquired.submodules:
            self.assertEqual(sub.bundle_sha256, "")
            self.assertFalse((self.package_dir / sub.bundle_relpath).exists())


class E2EStoredBundleTests(unittest.TestCase):
    """Replay side checks on an already stored source package."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.package_root = self.root / "package"
        (self.package_root / "bundles").mkdir(parents=True)
        self.bundle_path = self.package_root / "bundles" / "gonka.bundle"
        self.bundle_bytes = b"SYNTHETIC BUNDLE PAYLOAD\n"
        self.bundle_path.write_bytes(self.bundle_bytes)
        self.recorded_hash = hashlib.sha256(self.bundle_bytes).hexdigest()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _acquirer(self, runner):
        return SourceAcquirer(
            GitClient(runner=runner),
            package_dir=self.package_root,
            scratch_dir=self.root / "scratch",
        )

    def _record(self):
        return {
            "bundle_relpath": "bundles/gonka.bundle",
            "bundle_sha256": self.recorded_hash,
            "bundle_ref": bundle_ref("gonka"),
            "commit_sha": PINNED_SHA,
        }

    def test_a_single_flipped_byte_in_a_stored_bundle_is_detected(self):
        tampered = bytearray(self.bundle_bytes)
        tampered[0] ^= 0x01
        self.bundle_path.write_bytes(bytes(tampered))
        acquirer = self._acquirer(FakeGitRunner())

        with self.assertRaises(IntegrityError) as cm:
            acquirer.verify_stored_bundle(self._record(), package_root=self.package_root)

        self.assertIn("does not match the lock; the package was tampered with", str(cm.exception))
        self.assertNotEqual(sha256_path(self.bundle_path), self.recorded_hash)

    def test_a_bundle_referenced_by_the_lock_but_absent_from_the_package_is_detected(self):
        self.bundle_path.unlink()
        acquirer = self._acquirer(FakeGitRunner())

        with self.assertRaises(IntegrityError) as cm:
            acquirer.verify_stored_bundle(self._record(), package_root=self.package_root)

        self.assertIn("missing a bundle referenced by the lock", str(cm.exception))

    def test_a_bundle_that_does_not_carry_the_recorded_commit_is_rejected(self):
        runner = FakeGitRunner()
        runner.register_bundle(self.bundle_path, {bundle_ref("gonka"): OTHER_SHA})
        acquirer = self._acquirer(runner)

        with self.assertRaises(IntegrityError) as cm:
            acquirer.verify_stored_bundle(self._record(), package_root=self.package_root)

        self.assertIn("points at a different commit than the lock records", str(cm.exception))

    def test_a_bundle_that_does_not_carry_the_pinned_ref_is_rejected(self):
        runner = FakeGitRunner()
        runner.register_bundle(self.bundle_path, {"refs/heads/main": PINNED_SHA})
        acquirer = self._acquirer(runner)

        with self.assertRaises(IntegrityError) as cm:
            acquirer.verify_stored_bundle(self._record(), package_root=self.package_root)

        self.assertIn("does not carry the expected pinned ref", str(cm.exception))

    def test_a_stored_bundle_without_a_recorded_ref_is_rejected(self):
        record = self._record()
        del record["bundle_ref"]
        runner = FakeGitRunner()
        runner.register_bundle(self.bundle_path, {bundle_ref("gonka"): PINNED_SHA})
        acquirer = self._acquirer(runner)

        with self.assertRaises(IntegrityError) as cm:
            acquirer.verify_stored_bundle(record, package_root=self.package_root)

        self.assertIn("bundle_ref", str(cm.exception))

    def test_a_bundle_relpath_that_escapes_the_package_root_is_rejected(self):
        record = self._record()
        record["bundle_relpath"] = "../outside.bundle"
        acquirer = self._acquirer(FakeGitRunner())

        with self.assertRaises(PathSafetyError) as cm:
            acquirer.verify_stored_bundle(record, package_root=self.package_root)

        self.assertIn("must not traverse parent directories", str(cm.exception))


class E2EPathSafetyTests(unittest.TestCase):
    """``resolve_within`` is the only way a JSON supplied path becomes a Path."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.base = self.root / "package"
        self.base.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("private\n", encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _symlink(self, link, target):
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover
            self.skipTest(f"symlinks are not usable in this environment: {exc}")

    def test_a_relative_path_inside_the_root_is_accepted_unchanged(self):
        resolved = resolve_within(self.base, "bundles/gonka.bundle", what="bundle path")
        self.assertEqual(resolved, self.base / "bundles" / "gonka.bundle")

    def test_a_parent_directory_escape_is_rejected(self):
        for candidate in ("../outside/secret.txt", "bundles/../../outside/secret.txt"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(PathSafetyError) as cm:
                    resolve_within(self.base, candidate, what="bundle path")
                self.assertIn("must not traverse parent directories", str(cm.exception))

    def test_an_absolute_path_is_rejected(self):
        with self.assertRaises(PathSafetyError) as cm:
            resolve_within(self.base, "/etc/passwd", what="bundle path")
        self.assertIn("must be relative", str(cm.exception))

    def test_an_empty_path_is_rejected(self):
        with self.assertRaises(PathSafetyError) as cm:
            resolve_within(self.base, "   ", what="bundle path")
        self.assertIn("must be a non-empty relative path", str(cm.exception))

    def test_a_symlink_pointing_out_of_the_package_cannot_be_followed(self):
        self._symlink(self.base / "escape", self.outside)
        with self.assertRaises(PathSafetyError) as cm:
            resolve_within(self.base, "escape/secret.txt", what="submodule path")
        self.assertIn("escapes its permitted root", str(cm.exception))

    def test_a_symlink_inside_the_package_is_rejected_even_when_it_stays_inside(self):
        (self.base / "real").mkdir()
        (self.base / "real" / "file.txt").write_text("data\n", encoding="utf-8")
        self._symlink(self.base / "link", self.base / "real")

        with self.assertRaises(PathSafetyError) as cm:
            resolve_within(self.base, "link/file.txt", what="submodule path")
        self.assertIn("traverses a symlink", str(cm.exception))


class E2ECredentialHygieneTests(unittest.TestCase):
    """A token must never reach argv, an environment value or an error text."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-src-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.token = "ghp_" + "A" * 32
        self.secret_file = self.root / "token.txt"
        self.secret_file.write_text(self.token, encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_url_embedding_a_token_is_rejected_without_echoing_the_token(self):
        with self.assertRaises(SourceSpecError) as cm:
            validate_remote_url(f"https://x-access-token:{self.token}@github.com/acme/widgets.git")
        self.assertIn("must not embed credentials", str(cm.exception))
        self.assertNotIn(self.token, str(cm.exception))
        self.assertIn(REDACTED, str(cm.exception))

    def test_ssh_and_scp_style_remotes_are_rejected_in_favour_of_https(self):
        with self.assertRaises(SourceSpecError) as cm:
            validate_remote_url("git@github.com:acme/widgets.git")
        self.assertIn("SSH/SCP style remotes are not supported", str(cm.exception))

        with self.assertRaises(SourceSpecError) as cm:
            validate_remote_url("ssh://github.com/acme/widgets.git")
        self.assertIn("must start with https://", str(cm.exception))

    def test_redaction_removes_the_token_from_argv_and_from_free_text(self):
        argv = ["git", "fetch", f"https://x-access-token:{self.token}@github.com/acme/widgets.git"]
        rendered = " ".join(redact_argv(argv))
        self.assertNotIn(self.token, rendered)
        self.assertIn(REDACTED, rendered)
        self.assertNotIn(self.token, redact(f"remote: rejected token {self.token}"))

    def test_a_failing_git_command_reports_the_failure_without_the_token(self):
        stderr = (
            "fatal: Authentication failed for "
            f"'https://x-access-token:{self.token}@github.com/acme/widgets.git'"
        )
        runner = FakeGitRunner(fetch_returncode=128, fetch_stderr=stderr)
        client = GitClient(
            runner=runner,
            credentials=CredentialProvider(secret_file=self.secret_file),
            helper_dir=self.root / "git-helper",
        )

        with self.assertRaises(GitCommandError) as cm:
            client.run(["fetch", GONKA_REPO_URL, PINNED_SHA], allow_credentials=True, timeout=5.0)

        self.assertIn("git command failed", str(cm.exception))
        self.assertNotIn(self.token, str(cm.exception))

    def test_the_secret_value_is_never_placed_in_argv_or_in_the_environment(self):
        runner = FakeGitRunner()
        client = GitClient(
            runner=runner,
            credentials=CredentialProvider(secret_file=self.secret_file),
            helper_dir=self.root / "git-helper",
        )

        client.run(["fetch", GONKA_REPO_URL, PINNED_SHA], allow_credentials=True, timeout=5.0)

        self.assertTrue(runner.calls)
        for argv in runner.calls:
            self.assertNotIn(self.token, " ".join(argv))
        for env in runner.envs:
            for key, value in env.items():
                self.assertNotIn(self.token, str(value), msg=f"token leaked through {key}")
            # The secret is only ever referenced by path and read by the helper.
            self.assertEqual(env["E2E_GIT_SECRET_FILE"], str(self.secret_file))
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_an_authenticated_fetch_uses_an_existing_executable_helper_for_both_askpass_variables(self):
        runner = FakeGitRunner()
        helper_dir = self.root / "git-helper"
        credential = CredentialProvider(secret_file=self.secret_file, username="synthetic-user")
        client = GitClient(runner=runner, credentials=credential, helper_dir=helper_dir)

        client.run(["fetch", GONKA_REPO_URL, PINNED_SHA], allow_credentials=True, timeout=5.0)

        self.assertEqual(len(runner.envs), 1)
        env = runner.envs[0]
        helper = Path(env["GIT_ASKPASS"])
        self.assertEqual(helper.parent, helper_dir)
        self.assertEqual(env["SSH_ASKPASS"], str(helper))
        self.assertTrue(helper.is_file())
        if os.name != "nt":
            self.assertTrue(os.access(helper, os.X_OK))
        self.assertGreater(helper.stat().st_size, 0)
        self.assertNotIn(self.token, helper.read_text(encoding="utf-8"))
        self.assertEqual(env["E2E_GIT_USERNAME"], credential.username)
        self.assertEqual(env["E2E_GIT_SECRET_FILE"], str(self.secret_file))

    def test_a_local_operation_runs_without_any_credential_helper(self):
        runner = FakeGitRunner(head_sha=PINNED_SHA)
        client = GitClient(
            runner=runner,
            credentials=CredentialProvider(secret_file=self.secret_file),
            helper_dir=self.root / "git-helper",
        )

        client.run(["rev-parse", "HEAD"], timeout=5.0)

        env = runner.envs[-1]
        self.assertEqual(env["GIT_ASKPASS"], "/bin/false")
        self.assertNotIn("E2E_GIT_SECRET_FILE", env)
        self.assertNotIn("E2E_GIT_USERNAME", env)


if __name__ == "__main__":
    unittest.main()
