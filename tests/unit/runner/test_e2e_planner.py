"""Planning rejects unusable packages and owns only its temporary directories.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.execution.errors import IntegrityError, OutputCollisionError, SourceAcquisitionError
from forward_e2e.execution.gitio import GitClient
from forward_e2e.execution import planner
from forward_e2e.execution.planner import (
    EXTERNAL_TEST_DIRS,
    HARNESS_FILES,
    NETWORK_FILES,
    RUNNER_VERSION_FILE,
    VERIFIER_FILES,
    PlanRequest,
    RunnerLayout,
    build_plan,
    default_runner_root,
    hash_runner_files,
)
from forward_e2e.execution.runlock import RUN_LOCK_SCHEMA, load_run_lock
from forward_e2e.execution.sources import SourceKind, SourceSpec, sha256_path
from tests.unit.runner.support.fakes import (
    write_contracts_tree, write_gonka_tree, write_text_file,
)
from tests.unit.runner.support.git import FakeGitRunner

GONKA_SHA = "1" * 40
CONTRACTS_SHA = "2" * 40


class RunnerLayoutOnTheRealCheckoutTests(unittest.TestCase):
    """The checked-in tree must satisfy the layout the runner image asserts.

    ``RunnerLayout.assert_complete`` runs inside the image against ``/app``; a
    file added to one of the hashed lists but not committed would only be
    noticed at the first ``plan`` in a freshly built image. Checking the real
    checkout here moves that discovery to the offline suite. Only reads.
    """

    def test_the_real_checkout_is_a_complete_runner_layout_with_a_non_empty_version(self):
        layout = RunnerLayout(default_runner_root())
        layout.assert_complete()
        self.assertTrue(layout.runner_version())

    def test_every_hashed_runner_asset_is_a_regular_file_and_every_external_test_tree_a_directory(self):
        root = default_runner_root()
        for rel in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES + (RUNNER_VERSION_FILE,):
            with self.subTest(path=rel):
                path = root / rel
                self.assertFalse(path.is_symlink(), f"{rel} must be a regular file, not a link")
                self.assertTrue(path.is_file(), f"{rel} is listed for hashing but is not a file")
        for label, rel in EXTERNAL_TEST_DIRS:
            with self.subTest(tree=label):
                path = root / rel
                self.assertFalse(path.is_symlink(), f"{rel} must be a real directory, not a link")
                self.assertTrue(path.is_dir(), f"{rel} is listed as an external test tree but is not a directory")

    def test_a_missing_hashed_file_raises_instead_of_producing_a_shorter_hash(self):
        # Work on a copy: the real tree is never mutated by a unit test.
        root = default_runner_root()
        with tempfile.TemporaryDirectory(prefix="fe2e-test-layout-") as temp:
            copy_root = Path(temp).resolve()
            for rel in HARNESS_FILES:
                destination = copy_root / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, destination)
            # Path names and bytes are what is hashed, so the copy agrees with
            # the checkout; this is the baseline a missing file must break.
            complete = hash_runner_files(copy_root, HARNESS_FILES)
            self.assertEqual(complete, hash_runner_files(root, HARNESS_FILES))
            for rel in HARNESS_FILES:
                with self.subTest(missing=rel):
                    (copy_root / rel).unlink()
                    try:
                        with self.assertRaises(IntegrityError) as caught:
                            hash_runner_files(copy_root, HARNESS_FILES)
                        self.assertEqual(caught.exception.details["path"], rel)
                    finally:
                        shutil.copyfile(root / rel, copy_root / rel)
                    self.assertEqual(hash_runner_files(copy_root, HARNESS_FILES), complete)
            # Nor can a shorter *list* impersonate the full one: dropping a path
            # from the list changes the digest instead of being absorbed.
            shorter = hash_runner_files(copy_root, HARNESS_FILES[1:])
            self.assertNotEqual(shorter, complete)


class PlanningGitRunner(FakeGitRunner):
    """Exercise real acquisition while replacing only the Git process boundary."""

    def __call__(self, argv, **kwargs):
        subcommand = self.subcommand(argv)
        if subcommand == "bundle" and "create" in argv:
            # Distinct payloads expose accidental interchange of source bundles.
            role = Path(argv[argv.index("create") + 1]).stem
            self.bundle_bytes = f"SYNTHETIC {role} BUNDLE\n".encode()
        result = super().__call__(argv, **kwargs)
        if subcommand == "clone" and result.returncode == 0:
            target = Path(argv[-1])
            if target.name == "gonka":
                write_gonka_tree(target)
            else:
                write_contracts_tree(target)
        return result


class PlannerPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="a8-test-planner-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runner_root = self.root / "runner"
        self.runner_root.mkdir()
        (self.runner_root / "runner-source.json").write_text(json.dumps({
            "schema_version": 1, "repo_url": "https://example.org/runner.git",
            "commit_sha": "3" * 40, "tree_sha": "4" * 40,
        }), encoding="utf-8")
        for rel in HARNESS_FILES + VERIFIER_FILES + NETWORK_FILES:
            write_text_file(self.runner_root / rel, "# synthetic runner asset\n")
        write_text_file(
            self.runner_root / RUNNER_VERSION_FILE, "2.0.0-e2e-immutable-source\n"
        )
        for name, rel_dir in EXTERNAL_TEST_DIRS:
            write_text_file(
                self.runner_root / rel_dir / f"{name}.txt",
                f"# synthetic {name} asset\n",
            )
        self.request = PlanRequest(
            gonka=SourceSpec(role="gonka", kind=SourceKind.REMOTE,
                             commit_sha=GONKA_SHA, repo_url="https://example.org/gonka"),
            contracts=SourceSpec(role="contracts", kind=SourceKind.REMOTE,
                                 commit_sha=CONTRACTS_SHA, repo_url="https://example.org/contracts"),
            output_dir=self.root / "package", profile="smoke",
        )
        self.git_runner = PlanningGitRunner()
        self.temporary_directories = []

    def plan(self):
        factory = planner.TemporaryDirectory
        self.emitted = []

        def record_temporary_directory(*args, **kwargs):
            # Keep real allocation and context-manager cleanup. Retain handles
            # so garbage collection cannot hide a missing planner cleanup.
            directory = factory(*args, dir=self.root, **kwargs)
            self.temporary_directories.append(directory)
            self.addCleanup(directory.cleanup)
            return directory

        with patch.object(planner, "TemporaryDirectory", side_effect=record_temporary_directory):
            return build_plan(
                self.request, runner_root=self.runner_root,
                git=GitClient(runner=self.git_runner),
                env={"E2E_RUNNER_IMAGE_ID": "sha256:" + "a" * 64},
                emit=self.emitted.append,
            )

    def temporary_roots(self):
        return {Path(directory.name) for directory in self.temporary_directories}

    def test_a_successful_plan_retains_valid_bundles_but_removes_default_temporary_roots(self):
        result = self.plan()
        lock = load_run_lock(result.lock_path)
        self.assertEqual(lock.lock_sha256, result.lock.lock_sha256)
        self.assertEqual(lock.schema_version, RUN_LOCK_SCHEMA)
        self.assertNotIn("overlay", lock.to_dict())
        self.assertEqual(lock.runner["runner_version"], "2.0.0-e2e-immutable-source")
        self.assertTrue(lock.runner["runner_version_sha256"])
        self.assertEqual(lock.runner["commit_sha"], "3" * 40)
        self.assertEqual(lock.runner["tree_sha"], "4" * 40)
        self.assertEqual(lock.runner["repo_url"], "https://example.org/runner.git")
        self.assertEqual(
            set(lock.external_tests["trees"]),
            {name for name, _ in EXTERNAL_TEST_DIRS},
        )
        self.assertEqual(lock.source_policy["model"], "immutable-source")
        self.assertFalse(lock.source_policy["modifications_allowed"])
        self.assertEqual(
            lock.source_policy["phases"],
            ["before_build", "after_build", "after_execution"],
        )
        self.assertEqual({p.name for p in result.package_dir.iterdir()},
                         {"bundles", "run.lock.json"})
        entries = lock.source_package["entries"]
        self.assertEqual(len(entries), 2)
        self.assertCountEqual([entry["role"] for entry in entries], ["gonka", "contracts"])
        expected_shas = {"gonka": GONKA_SHA, "contracts": CONTRACTS_SHA}
        for entry in entries:
            role = entry["role"]
            self.assertEqual(entry["commit_sha"], expected_shas[role])
            self.assertEqual(entry["bundle_relpath"], f"bundles/{role}.bundle")
            self.assertEqual(entry["bundle_ref"], f"refs/e2e/source/{role}")
            bundle = result.package_dir / entry["bundle_relpath"]
            self.assertEqual(bundle.read_bytes(), f"SYNTHETIC {role} BUNDLE\n".encode())
            self.assertEqual(sha256_path(bundle), entry["bundle_sha256"])
        self.assertEqual(len(self.temporary_roots()), 2)
        for root in self.temporary_roots():
            self.assertFalse(root.exists())
            self.assertFalse(root.is_relative_to(result.package_dir))

    def test_each_missing_mandatory_network_file_is_rejected_before_source_acquisition(self):
        for rel in NETWORK_FILES:
            with self.subTest(path=rel):
                path = self.runner_root / rel
                original = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(IntegrityError):
                        self.plan()
                    self.assertEqual(self.git_runner.calls, [])
                    self.assertFalse((self.request.output_dir / "run.lock.json").exists())
                finally:
                    path.write_bytes(original)

    def test_a_missing_external_test_directory_is_rejected_before_source_acquisition(self):
        target_dir = self.runner_root / "harness/testermint"
        shutil.rmtree(target_dir)
        with self.assertRaises(IntegrityError):
            self.plan()
        self.assertEqual(self.git_runner.calls, [])
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_a_symlink_inside_an_external_test_tree_prevents_lock_publication_and_cleans_temporary_roots(self):
        outside = write_text_file(self.root / "outside.kt", "class Outside\n")
        (self.runner_root / "harness/testermint/Symlinked.kt").symlink_to(outside)
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())
        self.assertEqual(len(self.temporary_roots()), 2)
        for root in self.temporary_roots():
            self.assertFalse(root.exists())

    def test_a_symlink_directory_inside_external_tests_cannot_be_omitted_from_the_lock_hash(self):
        outside = self.root / "external-tests"
        write_text_file(outside / "Untracked.kt", "class Outside\n")
        (self.runner_root / "harness/testermint/linked-tests").symlink_to(
            outside, target_is_directory=True,
        )
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_a_symlink_external_test_tree_root_is_not_hashed_as_runner_owned(self):
        target = self.runner_root / "harness/testermint"
        outside = self.root / "external-tests"
        write_text_file(outside / "Untracked.kt", "class Outside\n")
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_a_required_harness_file_cannot_read_outside_runner_through_a_symlink(self):
        target = self.runner_root / HARNESS_FILES[0]
        outside = write_text_file(self.root / "outside-harness.py", "# foreign harness\n")
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_a_required_network_file_cannot_read_outside_runner_through_a_symlink(self):
        target = self.runner_root / NETWORK_FILES[0]
        outside = write_text_file(self.root / "outside-network.yml", "foreign: true\n")
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_unlisted_python_source_symlink_prevents_a_runner_source_lock(self):
        outside = write_text_file(self.root / "foreign.py", "# foreign runner code\n")
        (self.runner_root / "forward_e2e/suite/linked.py").symlink_to(outside)
        with self.assertRaises(IntegrityError) as caught:
            self.plan()
        self.assertIn("symlink", str(caught.exception))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())

    def test_exhausted_fetch_fallbacks_remove_both_temporary_roots_before_any_clone(self):
        # A failed remote leaves no commit object, including after fallback fetches.
        self.git_runner = PlanningGitRunner(
            fetch_returncode=128, fetch_stderr="synthetic unreachable remote",
            object_types=(None,),
        )
        with self.assertRaises(SourceAcquisitionError):
            self.plan()
        fetches = [argv for argv in self.git_runner.calls
                   if self.git_runner.subcommand(argv) == "fetch"]
        self.assertEqual(len(fetches), 3)
        self.assertEqual(fetches[0][-1], GONKA_SHA)
        self.assertIn("+refs/heads/*:refs/remotes/origin/*", fetches[1])
        self.assertIn("+refs/pull/*/head:refs/remotes/origin/pr/*", fetches[2])
        self.assertFalse(any(self.git_runner.subcommand(argv) == "clone"
                             for argv in self.git_runner.calls))
        self.assertFalse((self.request.output_dir / "run.lock.json").exists())
        self.assertEqual(len(self.temporary_roots()), 2)
        for root in self.temporary_roots():
            self.assertFalse(root.exists())

    def test_explicit_scratch_and_worktree_roots_remain_owned_by_the_caller(self):
        self.request.scratch_dir = self.root / "caller-scratch"
        self.request.worktree_root = self.root / "caller-worktrees"
        marker = write_text_file(self.request.scratch_dir / "keep.txt", "caller data\n")
        result = self.plan()
        self.assertTrue(result.gonka_source.worktree.is_dir())
        self.assertTrue(result.contracts_source.worktree.is_dir())
        self.assertEqual(marker.read_text(), "caller data\n")

    def test_a_regular_file_output_is_a_structured_usage_error_before_any_git_call(self):
        self.request.output_dir.write_text("existing user data\n")
        with self.assertRaises(OutputCollisionError) as caught:
            self.plan()
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(caught.exception.code, "OUTPUT_COLLISION")
        self.assertEqual(self.git_runner.calls, [])
        self.assertEqual(self.request.output_dir.read_text(), "existing user data\n")


    def test_a_canonical_scenario_request_produces_no_deprecation_notice(self):
        self.request.profile = None
        self.request.scenarios = ["usdt-withdrawal-failure-recovery,lock-exact-e"]
        result = self.plan()
        # Catalog order, not flag order: lock-exact-e precedes the R6.1 task.
        self.assertEqual(
            load_run_lock(result.lock_path).scenarios,
            ["lock-exact-e", "usdt-withdrawal-failure-recovery"],
        )
        self.assertEqual([m for m in self.emitted if m.startswith("notice:")], [])
