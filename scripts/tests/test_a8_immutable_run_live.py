"""run-live and build-external-harness under the immutable-source model.

The two Git snapshots (Gonka and Marketplace) are real throw-away repositories,
because the snapshot check measures them with ``git``. Cargo, Gradle, Docker
and the A9 artefact verifier are the external boundaries and are played by a
scripted runner that writes exactly what the real tools would write to the
paths the harness gave them. ``run_live`` / ``build_external_harness``
themselves are never mocked.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from scripts.tests import immutable_fixtures as fx
    from scripts.tests.support import EXPECTATION_ENV_NAMES, MODULE_PATH, a8, env_pairs
except ImportError:  # pragma: no cover - direct discovery from scripts/tests
    import immutable_fixtures as fx
    from support import EXPECTATION_ENV_NAMES, MODULE_PATH, a8, env_pairs

harness = a8.external_harness
SCENARIO = "lock-exact-e"
TEST_NAME = a8.LIVE_SCENARIO_TESTS[SCENARIO]
UNRELATED_SHA = "d" * 40


class SnapshotFixture(unittest.TestCase):
    """Two pristine snapshots, a runner-owned harness dir and an A9 package."""

    def setUp(self):
        env_patch = patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in (*EXPECTATION_ENV_NAMES, "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            os.environ.pop(name, None)
        tmp = tempfile.TemporaryDirectory(prefix="a8-immutable-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.gonka = self.root / "gonka"
        self.gonka_sha = fx.commit_repo(self.gonka, fx.gonka_files())
        self.market = self.root / "marketplace"
        self.market_sha = fx.commit_repo(self.market, fx.marketplace_files())
        self.harness_dir = fx.write_harness_dir(self.root / "runner" / "testermint")
        self.work_root = self.root / "work"
        self.evidence_root = self.root / "evidence"
        self.run_id = "r1"
        self.evidence_dir = self.evidence_root / self.run_id
        self.manifest = self.root / "a9" / "build-manifest.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps({"marketplace_commit_sha": self.market_sha}), "utf-8")

    # -- boundaries ---------------------------------------------------------

    def fake_a9(self, repo, manifest_path, runner):
        del repo, runner
        wasm = Path(manifest_path).parent / "wasm"
        wasm.mkdir(exist_ok=True)
        (wasm / "marketplace_deal.wasm").write_bytes(b"\x00asm-deal")
        (wasm / "marketplace_factory.wasm").write_bytes(b"\x00asm-factory")
        return wasm / "marketplace_deal.wasm", wasm / "marketplace_factory.wasm", {"marketplace_commit_sha": self.market_sha}

    def run_live(self, runner, *, scenario=SCENARIO, expected_gonka_sha=None, extra=()):
        args = a8.parser().parse_args(
            [
                "run-live",
                "--marketplace-dir", str(self.market),
                "--gonka-dir", str(self.gonka),
                "--expected-gonka-sha", expected_gonka_sha or self.gonka_sha,
                "--testermint-harness-dir", str(self.harness_dir),
                "--work-root", str(self.work_root),
                "--manifest", str(self.manifest),
                "--evidence-dir", str(self.evidence_root),
                "--run-id", self.run_id,
                "--scenario", scenario,
                *extra,
            ]
        )
        with patch.object(a8, "Runner", lambda: runner), patch.object(
            a8, "docker_resource_collisions", return_value={"containers": [], "volumes": []}
        ), patch.object(a8, "verified_release_artifacts", side_effect=self.fake_a9):
            a8.run_live(args)

    def build(self, runner, *, extra=()):
        args = a8.parser().parse_args(
            [
                "build-external-harness",
                "--gonka-dir", str(self.gonka),
                "--work-root", str(self.work_root),
                "--testermint-harness-dir", str(self.harness_dir),
                *extra,
            ]
        )
        with patch.object(a8, "Runner", lambda: runner):
            a8.build_external_harness(args)

    # -- evidence readers ---------------------------------------------------

    def immutability(self, evidence_dir=None):
        return json.loads(((evidence_dir or self.evidence_dir) / "source-immutability.json").read_text("utf-8"))

    def gradle_test_command(self, runner):
        commands = [call for call in runner.calls if "--tests" in call]
        self.assertEqual(len(commands), 1)
        return commands[0]


class ClasspathDownloadFailureTests(SnapshotFixture):
    # Producer shape: Gradle wrapper Download.java emits this before Gradle
    # starts. Values are synthetic; no distribution is actually downloaded.
    WRAPPER_TIMEOUT = (
        'Exception in thread "main" java.io.IOException: Downloading from '
        'https://services.gradle.org/distributions/gradle-8.8-bin.zip failed: timeout\n'
        '\tat org.gradle.wrapper.Download.downloadInternal(Download.java:122)\n'
    )

    def prepare(self, failures):
        layout = harness.WorkLayout(self.work_root)
        layout.create()
        runner = fx.ScriptedRunner()
        original_run = runner.run
        responses = iter(failures)
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs["timeout"]))
            response = next(responses, None)
            if response is not None:
                return a8.CommandResult(1, "", response)
            return original_run(argv, **kwargs)

        runner.run = run
        return layout, runner, calls

    def export(self, layout, runner):
        return a8.export_upstream_classpath(
            runner, self.gonka, self.harness_dir, layout, self.evidence_dir, 60,
        )

    def test_wrapper_download_timeout_is_retried_before_any_live_scenario_and_all_attempts_are_logged(self):
        layout, runner, calls = self.prepare([self.WRAPPER_TIMEOUT])
        with patch.object(a8.time, "sleep") as sleep:
            self.assertEqual(self.export(layout, runner), layout.classpath_file)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("a8ExportClasspath" in command for command, _ in calls))
        self.assertLessEqual(calls[1][1], calls[0][1])
        sleep.assert_called_once_with(2.0)
        log = (self.evidence_dir / "upstream-classpath.log").read_text()
        self.assertIn(self.WRAPPER_TIMEOUT, log)
        self.assertIn("attempt 2/3", log)
        self.assertNotIn("--tests", calls[0][0])

    def test_repeated_wrapper_timeouts_stop_after_three_preparation_attempts(self):
        layout, runner, calls = self.prepare([self.WRAPPER_TIMEOUT] * 3)
        with patch.object(a8.time, "sleep") as sleep:
            with self.assertRaisesRegex(a8.AcceptanceError, "classpath export failed"):
                self.export(layout, runner)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(layout.classpath_file.exists())

    def test_compilation_failure_and_non_wrapper_network_timeout_are_not_retried(self):
        for output in ("Compilation failed", "java.net.SocketTimeoutException: Read timed out"):
            with self.subTest(output=output):
                layout, runner, calls = self.prepare([output])
                with patch.object(a8.time, "sleep") as sleep:
                    with self.assertRaisesRegex(a8.AcceptanceError, "classpath export failed"):
                        self.export(layout, runner)
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_wrapper_timeout_cannot_extend_the_original_preparation_budget(self):
        layout, runner, calls = self.prepare([self.WRAPPER_TIMEOUT])
        with patch.object(a8.time, "monotonic", side_effect=[0.0, 0.0, 59.0]), patch.object(a8.time, "sleep") as sleep:
            with self.assertRaisesRegex(a8.AcceptanceError, "classpath export failed"):
                self.export(layout, runner)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_wrapper_timeout_with_an_already_produced_classpath_is_not_retried(self):
        layout, runner, calls = self.prepare([self.WRAPPER_TIMEOUT])
        layout.classpath_file.write_text("synthetic already-produced output\n")
        with patch.object(a8.time, "sleep") as sleep:
            with self.assertRaisesRegex(a8.AcceptanceError, "classpath export failed"):
                self.export(layout, runner)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


class RunLiveHappyPathTests(SnapshotFixture):
    def test_two_scenarios_share_only_the_explicit_run_gradle_home_and_keep_separate_build_outputs(self):
        cache = self.root / "suite-build" / "gradle-home"
        commands = []
        for number in (1, 2):
            self.run_id = f"shared-{number}"
            self.work_root = self.root / f"task-{number}"
            runner = fx.ScriptedRunner()
            self.run_live(runner, extra=("--gradle-user-home", str(cache)))
            gradle = [call for call in runner.calls if "org.gradle.wrapper.GradleWrapperMain" in call]
            self.assertTrue(gradle)
            for command in gradle:
                self.assertEqual(env_pairs(command)["GRADLE_USER_HOME"], str(cache))
                self.assertIn("--no-build-cache", command)
                self.assertIn("--no-configuration-cache", command)
                self.assertIn(str(self.work_root), " ".join(command))
            commands.append(gradle)
        self.assertTrue(cache.is_dir())
        self.assertTrue((self.root / "task-1" / "testermint-classpath.txt").is_file())
        self.assertTrue((self.root / "task-2" / "testermint-classpath.txt").is_file())
        self.assertNotEqual(commands[0], commands[1])

    def test_a_shared_gradle_home_inside_a_product_snapshot_is_refused_before_it_is_created(self):
        cache = self.gonka / "shared-gradle"
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.AcceptanceError):
            self.run_live(runner, extra=("--gradle-user-home", str(cache)))
        self.assertFalse(cache.exists())
        self.assertEqual(runner.calls, [])

    def test_network_root_nested_in_selected_snapshot_is_refused(self):
        network_root = self.gonka / "nested-work" / "network-root"

        with self.assertRaises(harness.ExternalHarnessError) as cm:
            harness.prepare_network_root(
                self.gonka,
                network_root,
                harness.DEFAULT_NETWORK_TEMPLATES_DIR,
                b3=False,
            )

        self.assertEqual(cm.exception.code, harness.CODE_WORK_ROOT_INSIDE_SNAPSHOT)
        self.assertFalse(network_root.exists())

    def test_network_root_symlink_into_an_empty_snapshot_directory_is_refused_before_copy(self):
        empty_snapshot_dir = self.gonka / "empty-network-target"
        empty_snapshot_dir.mkdir()
        network_root = self.work_root / "network-root"
        network_root.parent.mkdir(parents=True, exist_ok=True)
        try:
            network_root.symlink_to(empty_snapshot_dir, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")

        with self.assertRaises(harness.ExternalHarnessError) as cm:
            harness.prepare_network_root(
                self.gonka,
                network_root,
                harness.DEFAULT_NETWORK_TEMPLATES_DIR,
                b3=False,
            )

        self.assertEqual(cm.exception.code, harness.CODE_NETWORK_ROOT_SYMLINK)
        self.assertEqual(list(empty_snapshot_dir.iterdir()), [])

    def test_network_root_integrity_detects_symlink_substitution_after_copy(self):
        network_root = self.work_root / "network-root"
        manifest = harness.prepare_network_root(
            self.gonka,
            network_root,
            harness.DEFAULT_NETWORK_TEMPLATES_DIR,
            b3=False,
        )
        saved_copy = self.work_root / "saved-network-root"
        network_root.rename(saved_copy)
        try:
            network_root.symlink_to(saved_copy, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")

        verification = harness.verify_network_root_integrity(network_root, manifest)

        self.assertEqual(verification["verdict"], "VIOLATED")
        self.assertEqual(verification["violations"][0]["code"], harness.CODE_NETWORK_ROOT_SYMLINK)

    def test_a_tracked_legacy_go_file_from_the_selected_commit_is_not_an_overlay_violation(self):
        # Gonka c33c9eaa contains this path; only an untracked addition is an overlay.
        self.gonka_sha = fx.commit_repo(
            self.gonka, {"inference-chain/app/legacy.go": "package app\n"},
            message="selected Gonka includes legacy.go",
        )
        self.run_live(fx.ScriptedRunner())
        self.assertEqual(self.immutability()["roots"]["gonka"]["verdict"], "UNCHANGED")

    def test_a_pristine_run_passes_and_records_both_snapshots_unchanged(self):
        runner = fx.ScriptedRunner()
        self.run_live(runner)

        record = self.immutability()
        self.assertEqual(record["schema"], a8.SOURCE_IMMUTABILITY_NETWORK_SCHEMA)
        self.assertEqual(record["verdict"], "UNCHANGED")
        self.assertEqual(set(record["roots"]), {"gonka", "marketplace"})
        self.assertEqual(record["roots"]["gonka"]["expected_sha"], self.gonka_sha)
        self.assertEqual(record["roots"]["marketplace"]["expected_sha"], self.market_sha)

    def test_the_live_context_source_carries_the_immutable_model_and_tree_shas(self):
        self.run_live(fx.ScriptedRunner())
        context = json.loads((self.evidence_dir / "live-context.json").read_text("utf-8"))
        source = context["source"]
        self.assertEqual(source["evidence_model"], a8.EVIDENCE_MODEL_IMMUTABLE)
        self.assertEqual(source["gonka_sha"], self.gonka_sha)
        self.assertEqual(source["gonka_tree_sha"], fx.git(self.gonka, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(source["marketplace_tree_sha"], fx.git(self.market, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(source["source_immutability_verdict"], "UNCHANGED")
        self.assertEqual(
            source["source_immutability_sha256"],
            hashlib.sha256((self.evidence_dir / "source-immutability.json").read_bytes()).hexdigest(),
        )
        self.assertNotIn("gonka_prepared_sha", source)
        self.assertEqual(context["command"], source["command"])
        self.assertEqual(source["command"]["test"], TEST_NAME)

    def test_every_external_harness_evidence_file_is_written_outside_the_snapshots(self):
        self.run_live(fx.ScriptedRunner())
        external = self.evidence_dir / "external-harness"
        for name in (
            "api-compat.json",
            "testermint-classpath.txt",
            "testermint-classpath.txt.json",
            "harness-inputs.json",
            "upstream-classpath.log",
        ):
            self.assertTrue((external / name).is_file(), name)
        self.assertTrue((self.evidence_dir / "network" / "network-manifest.json").is_file())
        self.assertTrue((self.evidence_dir / "testermint-junit" / "junit-summary.json").is_file())
        inputs = json.loads((external / "harness-inputs.json").read_text("utf-8"))
        self.assertIn("classpath_metadata", inputs)
        self.assertEqual(
            json.loads((external / "api-compat.json").read_text("utf-8"))["verdict"], "COMPATIBLE"
        )

    def test_testermint_is_pointed_at_the_network_root_and_never_at_the_gonka_snapshot(self):
        runner = fx.ScriptedRunner()
        self.run_live(runner)
        command = self.gradle_test_command(runner)
        env = env_pairs(command)
        network_root = self.work_root / "network-root"
        self.assertEqual(env["GONKA_REPO_ROOT"], str(network_root))
        self.assertTrue((network_root / "local-test-net" / "a8-ownership.yml").is_file())
        overrides = "inference-chain/test_genesis_overrides.json"
        self.assertEqual(
            (network_root / overrides).read_bytes(),
            (self.gonka / overrides).read_bytes(),
        )
        manifest = json.loads((self.evidence_dir / "network" / "network-manifest.json").read_text("utf-8"))
        self.assertIn(overrides, [item["path"] for item in manifest["upstream_copies"]])
        self.assertEqual(env["A8_OWNERSHIP_LABEL"], "io.gonka.a8.run-id")
        self.assertEqual(env["A8_CONTAINER_CONTROL_STATE_DIR"], str(self.evidence_dir / "container-control"))
        self.assertEqual(env["GRADLE_USER_HOME"], str(self.work_root / "gradle-home"))
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_WORK_TREE", env)
        unset = {command[i + 1] for i, item in enumerate(command[:-1]) if item == "-u"}
        self.assertEqual(command[0], "env")
        self.assertLessEqual({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", *a8.LEGACY_OVERLAY_ENVIRONMENT}, unset)

    def test_the_harness_that_testermint_re_enters_is_the_runners_own_script(self):
        runner = fx.ScriptedRunner()
        self.run_live(runner)
        env = env_pairs(self.gradle_test_command(runner))
        self.assertEqual(env["A8_HARNESS"], str(MODULE_PATH))
        self.assertNotEqual(env["A8_HARNESS"], str(self.market / "scripts" / "a8_acceptance.py"))
        self.assertEqual(env["A8_MARKETPLACE_DIR"], str(self.market))

    def test_gradle_runs_the_external_project_with_the_upstream_wrapper_jar_and_no_gradlew_copy(self):
        runner = fx.ScriptedRunner()
        self.run_live(runner)
        command = self.gradle_test_command(runner)
        java = command.index("java")
        self.assertEqual(
            command[java:java + 4],
            ["java", "-cp", str(self.gonka / "testermint/gradle/wrapper/gradle-wrapper.jar"), "org.gradle.wrapper.GradleWrapperMain"],
        )
        self.assertEqual(command[command.index("--project-dir") + 1], str(self.harness_dir))
        self.assertEqual(command[command.index("--tests") + 1], TEST_NAME)
        self.assertFalse((self.gonka / "testermint" / "gradlew.a8-linux").exists())
        self.assertNotIn("bash", command)

    def test_the_test_wasm_fixtures_are_built_into_the_work_root_not_the_contracts_checkout(self):
        runner = fx.ScriptedRunner()
        self.run_live(runner)
        cargo = next(call for call in runner.calls if call[:2] == ["cargo", "build"])
        self.assertEqual(cargo[cargo.index("--target-dir") + 1], str(self.work_root / "wasm-target"))
        self.assertEqual(cargo[cargo.index("--manifest-path") + 1], str(self.market / "Cargo.toml"))
        self.assertFalse((self.market / "target").exists())

    def test_a_newly_tracked_gonka_file_in_an_arbitrary_directory_and_a_submodule_file_are_copied_into_network_root_without_runner_changes(self):
        sub_origin = self.root / "sub-origin-fullcopy"
        fx.commit_repo(sub_origin, {"proto/submodule_service.proto": "syntax = \"proto3\";\n"})
        fx.git(self.gonka, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "vendor/sub")
        arbitrary_rel = "public-html/assets/custom-config.json"
        arbitrary_file = self.gonka / arbitrary_rel
        arbitrary_file.parent.mkdir(parents=True, exist_ok=True)
        arbitrary_file.write_text('{"feature": true}\n', "utf-8")
        fx.git(self.gonka, "add", arbitrary_rel)
        fx.git(self.gonka, "commit", "-q", "-m", "add arbitrary tracked file and submodule")
        self.gonka_sha = fx.git(self.gonka, "rev-parse", "HEAD")

        self.run_live(fx.ScriptedRunner())

        network_root = self.work_root / "network-root"
        self.assertEqual((network_root / arbitrary_rel).read_text("utf-8"), '{"feature": true}\n')
        self.assertEqual(
            (network_root / "vendor/sub/proto/submodule_service.proto").read_text("utf-8"),
            'syntax = "proto3";\n',
        )
        self.assertFalse((network_root / ".git").exists())
        self.assertFalse((network_root / "vendor/sub/.git").exists())
        manifest = json.loads((self.evidence_dir / "network" / "network-manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["copy_mode"], "full_working_tree")
        copied_paths = {item["path"] for item in manifest["upstream_copies"]}
        self.assertIn(arbitrary_rel, copied_paths)
        self.assertIn("vendor/sub/proto/submodule_service.proto", copied_paths)
        self.assertEqual(manifest["post_run_verification"]["verdict"], "UNCHANGED")


class RunLivePristineRefusalTests(SnapshotFixture):
    """Each mutation is one fact on top of the pristine fixture; nothing is built."""

    def assert_refused(self, code, *, label="gonka"):
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.HarnessRefusal) as cm:
            self.run_live(runner)
        self.assertEqual(runner.calls, [], "no build step may run on a non-pristine snapshot")
        record = self.immutability()
        self.assertEqual(record["verdict"], "VIOLATED")
        codes = {item["code"] for item in record["roots"][label]["violations"]}
        self.assertIn(code, codes)
        self.assertIn(code, str(cm.exception))
        self.assertFalse((self.work_root / "network-root").exists())
        return record

    def test_a_changed_tracked_gonka_file_is_refused_before_any_build(self):
        (self.gonka / fx.GONKA_DOCKER_GROUP).write_text("changed\n", "utf-8")
        self.assert_refused("SOURCE_TREE_DIRTY")

    def test_a_deleted_tracked_gonka_file_is_refused_before_any_build(self):
        (self.gonka / "local-test-net" / "dns" / "Corefile").unlink()
        self.assert_refused("SOURCE_TREE_DIRTY")

    def test_an_added_untracked_gonka_file_is_refused_before_any_build(self):
        (self.gonka / "local-test-net" / "docker-compose.extra.yml").write_text("services: {}\n", "utf-8")
        self.assert_refused("SOURCE_TREE_DIRTY")

    def test_a_planted_binary_hidden_by_gitignore_is_still_refused(self):
        (self.gonka / "build").mkdir()
        (self.gonka / "build" / "inferenced").write_bytes(b"\x7fELF planted")
        record = self.assert_refused("SOURCE_TREE_DIRTY")
        status = [s for v in record["roots"]["gonka"]["violations"] for s in v.get("status", [])]
        self.assertTrue(any("build/" in item for item in status), status)

    def test_the_old_gradlew_copy_in_testermint_is_refused_as_a_forbidden_file(self):
        (self.gonka / "testermint" / "gradlew.a8-linux").write_text("#!/bin/sh\n", "utf-8")
        self.assert_refused("SOURCE_FORBIDDEN_FILE")

    def test_an_overlay_attempt_copying_the_marketplace_test_into_gonka_is_refused(self):
        target = self.gonka / "testermint/src/test/kotlin/MarketplaceContractAcceptanceTests.kt"
        target.write_text("class MarketplaceContractAcceptanceTests\n", "utf-8")
        self.assert_refused("SOURCE_FORBIDDEN_FILE")

    def test_a_gonka_checkout_at_another_commit_than_the_selected_one_is_refused(self):
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.HarnessRefusal) as cm:
            self.run_live(runner, expected_gonka_sha=UNRELATED_SHA)
        self.assertEqual(cm.exception.code, "SOURCE_HEAD_MISMATCH")
        self.assertEqual(runner.calls, [])

    def test_a_dirty_marketplace_snapshot_is_refused_like_a_dirty_gonka_snapshot(self):
        (self.market / "Cargo.toml").write_text("[workspace]\n", "utf-8")
        self.assert_refused("SOURCE_TREE_DIRTY", label="marketplace")

    def test_a_submodule_checked_out_at_a_commit_other_than_the_pinned_one_is_refused(self):
        sub_origin = self.root / "sub-origin"
        pinned = fx.commit_repo(sub_origin, {"README": "one\n"})
        drifted = fx.commit_repo(sub_origin, {"README": "two\n"}, "second")
        fx.git(sub_origin, "checkout", "-q", pinned)
        fx.git(self.gonka, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "vendor/sub")
        fx.git(self.gonka, "commit", "-q", "-m", "add submodule")
        self.gonka_sha = fx.git(self.gonka, "rev-parse", "HEAD")
        fx.git(self.gonka / "vendor" / "sub", "checkout", "-q", drifted)
        self.assert_refused("SOURCE_SUBMODULE_DRIFT")

    def test_an_unmaterialized_submodule_is_refused_before_any_build(self):
        sub_origin = self.root / "sub-origin"
        fx.commit_repo(sub_origin, {"README": "one\n"})
        fx.git(self.gonka, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "vendor/sub")
        fx.git(self.gonka, "commit", "-q", "-m", "add submodule")
        self.gonka_sha = fx.git(self.gonka, "rev-parse", "HEAD")
        fx.git(self.gonka, "submodule", "deinit", "-f", "vendor/sub")
        self.assert_refused("SOURCE_SUBMODULE_DRIFT")

    def test_an_ignored_file_inside_a_submodule_is_refused_before_any_build(self):
        sub_origin = self.root / "sub-origin"
        fx.commit_repo(sub_origin, {".gitignore": "build/\n", "README": "one\n"})
        fx.git(self.gonka, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "vendor/sub")
        fx.git(self.gonka, "commit", "-q", "-m", "add submodule")
        self.gonka_sha = fx.git(self.gonka, "rev-parse", "HEAD")
        planted = self.gonka / "vendor" / "sub" / "build" / "inferenced"
        planted.parent.mkdir()
        planted.write_bytes(b"\x7fELF planted")
        self.assert_refused("SOURCE_TREE_DIRTY")

    def test_the_legacy_prepared_sha_environment_is_refused_before_any_measurement(self):
        for name in a8.LEGACY_OVERLAY_ENVIRONMENT:
            with self.subTest(variable=name):
                os.environ[name] = "b" * 40 if "SHA" in name else "inference-chain/app/legacy.go"
                runner = fx.ScriptedRunner()
                with self.assertRaisesRegex(a8.AcceptanceError, name):
                    self.run_live(runner)
                self.assertEqual(runner.calls, [])
                self.assertFalse(self.evidence_dir.exists())
                del os.environ[name]

    def test_a_work_root_inside_the_gonka_snapshot_is_refused(self):
        self.work_root = self.gonka / "a8-work"
        with self.assertRaises(a8.HarnessRefusal) as cm:
            self.run_live(fx.ScriptedRunner())
        self.assertEqual(cm.exception.code, "WORK_ROOT_INSIDE_SNAPSHOT")
        self.assertFalse(self.work_root.exists())


class RunLiveMutationDuringRunTests(SnapshotFixture):
    def test_a_tracked_gonka_file_changed_during_the_scenario_fails_the_run_and_keeps_evidence(self):
        def mutate(_env):
            (self.gonka / "testermint/src/test/kotlin/TestermintTest.kt").write_text("tampered\n", "utf-8")

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.run_live(fx.ScriptedRunner(during_test=mutate))
        record = self.immutability()
        self.assertEqual(record["verdict"], "VIOLATED")
        self.assertEqual(record["roots"]["marketplace"]["verdict"], "UNCHANGED")
        fields = {item["field"] for item in record["roots"]["gonka"]["differences"]}
        self.assertIn("tracked_digest", fields)
        self.assertTrue((self.evidence_dir / "testermint-junit" / "junit-summary.json").is_file())
        context = json.loads((self.evidence_dir / "live-context.json").read_text("utf-8"))
        self.assertEqual(context["source"]["source_immutability_verdict"], "VIOLATED")

    def test_a_gradlew_copy_written_into_gonka_during_the_scenario_fails_the_run(self):
        def plant(_env):
            (self.gonka / "testermint" / "gradlew.a8-linux").write_text("#!/bin/sh\n", "utf-8")

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.run_live(fx.ScriptedRunner(during_test=plant))
        codes = {v["code"] for v in self.immutability()["roots"]["gonka"]["violations"]}
        self.assertIn("SOURCE_FORBIDDEN_FILE", codes)

    def test_a_marketplace_file_added_during_the_scenario_fails_the_run(self):
        def plant(_env):
            (self.market / "target").mkdir()
            (self.market / "target" / "leftover.wasm").write_bytes(b"\x00asm")

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.run_live(fx.ScriptedRunner(during_test=plant))
        self.assertEqual(self.immutability()["roots"]["marketplace"]["verdict"], "VIOLATED")

    def test_a_failing_gradle_run_still_measures_both_snapshots_afterwards(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "Testermint marketplace scenario failed"):
            self.run_live(fx.ScriptedRunner(test_returncode=1, junit_outcome="failed"))
        self.assertEqual(self.immutability()["verdict"], "UNCHANGED")

    def test_a_mutated_working_copy_file_in_network_root_during_the_scenario_invalidates_evidence_even_when_the_reference_gonka_checkout_stays_clean(self):
        def mutate_working_copy(env):
            network_root = Path(env["GONKA_REPO_ROOT"])
            (network_root / "inference-chain/test_genesis_overrides.json").write_text('{"tampered": true}\n', "utf-8")

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.run_live(fx.ScriptedRunner(during_test=mutate_working_copy))
        record = self.immutability()
        self.assertEqual(record["roots"]["gonka"]["verdict"], "UNCHANGED")
        self.assertEqual(record["roots"]["marketplace"]["verdict"], "UNCHANGED")
        self.assertEqual(record["network_root"]["verdict"], "VIOLATED")
        self.assertEqual(record["verdict"], "VIOLATED")
        context = json.loads((self.evidence_dir / "live-context.json").read_text("utf-8"))
        self.assertEqual(context["source"]["source_immutability_verdict"], "VIOLATED")

    def test_a_deleted_working_copy_file_in_network_root_during_the_scenario_invalidates_evidence_even_when_the_reference_gonka_checkout_stays_clean(self):
        def delete_working_copy_file(env):
            network_root = Path(env["GONKA_REPO_ROOT"])
            (network_root / "local-test-net/dns/Corefile").unlink()

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.run_live(fx.ScriptedRunner(during_test=delete_working_copy_file))
        record = self.immutability()
        self.assertEqual(record["roots"]["gonka"]["verdict"], "UNCHANGED")
        self.assertEqual(record["network_root"]["verdict"], "VIOLATED")
        self.assertEqual(record["verdict"], "VIOLATED")

    def test_runtime_additions_under_prod_local_in_network_root_are_recorded_while_original_working_copy_files_and_checkouts_stay_unchanged(self):
        def write_runtime_data(env):
            network_root = Path(env["GONKA_REPO_ROOT"])
            state_file = network_root / "prod-local/genesis/runtime-state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text('{"height": 42}\n', "utf-8")

        self.run_live(fx.ScriptedRunner(during_test=write_runtime_data))
        record = self.immutability()
        self.assertEqual(record["verdict"], "UNCHANGED")
        self.assertEqual(record["network_root"]["verdict"], "UNCHANGED")
        self.assertIn("prod-local/genesis/runtime-state.json", record["network_root"]["runtime_additions"])


class RunLiveApiAndJunitTests(SnapshotFixture):
    def test_a_missing_upstream_api_is_refused_before_any_network_or_gradle_run(self):
        # Exactly one fact: getRepoRoot no longer honours GONKA_REPO_ROOT.
        docker_group = self.gonka / fx.GONKA_DOCKER_GROUP
        docker_group.write_text(
            docker_group.read_text("utf-8").replace(
                '    System.getenv("GONKA_REPO_ROOT")?.takeIf { it.isNotBlank() }?.let { return it }\n', ""
            ),
            "utf-8",
        )
        fx.git(self.gonka, "commit", "-qam", "drop override")
        self.gonka_sha = fx.git(self.gonka, "rev-parse", "HEAD")
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.HarnessRefusal) as cm:
            self.run_live(runner)
        self.assertEqual(cm.exception.code, "TESTERMINT_API_MISSING")
        self.assertIn("getRepoRoot.GONKA_REPO_ROOT", str(cm.exception))
        compat = json.loads((self.evidence_dir / "external-harness" / "api-compat.json").read_text("utf-8"))
        self.assertEqual(compat["verdict"], "INCOMPATIBLE")
        self.assertEqual([item["id"] for item in compat["missing"]], ["getRepoRoot.GONKA_REPO_ROOT"])
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.work_root / "network-root").exists())

    def test_a_green_gradle_run_whose_junit_lacks_the_selected_test_fails(self):
        other = a8.LIVE_SCENARIO_TESTS["lock-e-plus-4"]
        with self.assertRaisesRegex(a8.AcceptanceError, "SELECTED_TEST_NOT_EXECUTED"):
            self.run_live(fx.ScriptedRunner(junit_test=other))
        summary = json.loads((self.evidence_dir / "testermint-junit" / "junit-summary.json").read_text("utf-8"))
        self.assertEqual(summary["executed"], 0)

    def test_a_skipped_selected_test_is_not_an_executed_test(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "SELECTED_TEST_NOT_EXECUTED"):
            self.run_live(fx.ScriptedRunner(junit_outcome="skipped"))

    def test_a_green_gradle_run_without_any_junit_report_fails(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "SELECTED_TEST_NOT_EXECUTED"):
            self.run_live(fx.ScriptedRunner(write_junit=False))

    def test_a_green_run_that_never_wrote_the_live_context_fails(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "live context .* was not written"):
            self.run_live(fx.ScriptedRunner(write_context=False))


class RunLiveB3GenesisTests(SnapshotFixture):
    B3 = "b3-foreign-native"

    def test_genesis_provisioner_disables_xtrace_before_reading_tgbot_password(self):
        repo = Path(__file__).resolve().parents[2]
        provisioner = (
            repo / "ops/a8/harness/network/genesis/a8-genesis-provision.sh"
        ).read_text(encoding="utf-8")
        compose = (repo / "ops/a8/harness/network/a8-b3-genesis.yml").read_text(
            encoding="utf-8"
        )
        self.assertTrue(provisioner.startswith("#!/bin/sh\n"))
        self.assertIn(
            'command: ["/bin/sh", "/a8-provision/a8-genesis-provision.sh"]',
            compose,
        )
        secret_block = provisioner.split(
            'if [ "$INIT_TGBOT" = "true" ]; then', 1
        )[1].split("else\n    echo \"INIT_TGBOT", 1)[0]
        self.assertLess(secret_block.index("set +x"), secret_block.index("$TGBOT_PRIVATE_KEY_PASS"))
        self.assertIn("printf '%s\\n' \"$TGBOT_PRIVATE_KEY_PASS\" | inferenced keys import", secret_block)
        self.assertGreater(secret_block.index("set -x"), secret_block.index("inferenced keys import"))

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "requires /bin/sh")
    def test_provisioner_config_keys_and_function_scope_work_in_sh_without_bash(self):
        script = Path(__file__).resolve().parents[2] / "ops/a8/harness/network/genesis/a8-genesis-provision.sh"
        source = script.read_text(encoding="utf-8")
        subprocess.run(["/bin/sh", "-n", str(script)], check=True, capture_output=True)
        config_block = source.split("# Process CONFIG_ environment variables\n", 1)[1].split(
            "# Check and apply config overrides", 1
        )[0]
        function_block = source.split("modify_genesis_file()", 1)[1].split("# Usage", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".inference/config").mkdir(parents=True)
            (root / ".inference/config/genesis.json").write_text('{}\n', encoding="utf-8")
            (root / "override.json").write_text('{"synthetic":true}\n', encoding="utf-8")
            shell = (
                'set -e\nHOME="$1"\n'
                'jq() { cat "$3"; }\nwhich() { return 0; }\n'
                'filter_cw20_code() { cat; }\n'
                + "modify_genesis_file()" + function_block
                + 'json_file=outer_json\noverride_file=outer_override\n'
                + 'modify_genesis_file "$HOME/override.json"\n'
                + '[ "$json_file" = outer_json ]\n[ "$override_file" = outer_override ]\n'
                + 'record_config() { printf "CONFIG_RESULT:%s=%s\\n" "$4" "$5"; }\n'
                + 'APP_NAME=record_config\n' + config_block
            )
            env = {key: value for key, value in os.environ.items() if not key.startswith("CONFIG_")}
            env["CONFIG_consensus__timeout__commit"] = "750ms"
            result = subprocess.run(
                ["/bin/sh", "-s", "--", tmp], input=shell, text=True,
                capture_output=True, env=env, check=True,
            )
            self.assertIn("CONFIG_RESULT:consensus.timeout.commit=750ms", result.stdout)
            self.assertEqual(
                json.loads((root / ".inference/config/genesis.json").read_text()),
                {"synthetic": True},
            )

    def setUp(self):
        super().setUp()
        synthetic = hashlib.sha256((self.gonka / harness.UPSTREAM_GENESIS_SCRIPT).read_bytes()).hexdigest()
        pin = patch.object(harness, "GENESIS_PROVISIONER_UPSTREAM_SHA256", synthetic)
        pin.start()
        self.addCleanup(pin.stop)

    def test_the_b3_scenario_passes_with_an_exact_genesis_delta_and_runner_compose_file(self):
        self.run_live(fx.ScriptedRunner(b3_writer=fx.write_b3_provision), scenario=self.B3)
        verification = json.loads((self.evidence_dir / "genesis" / "b3-genesis-verification.json").read_text("utf-8"))
        self.assertEqual(verification["verdict"], "PASS", verification["findings"])
        self.assertEqual(verification["command"], fx.B3_COMMAND)
        manifest = json.loads((self.evidence_dir / "network" / "network-manifest.json").read_text("utf-8"))
        self.assertIn("local-test-net/a8-b3-genesis.yml", manifest["compose_files_by_pair"]["genesis"])
        self.assertNotIn("local-test-net/a8-b3-genesis.yml", manifest["compose_files_by_pair"]["join1"])
        self.assertTrue((self.work_root / "network-root" / "a8" / "genesis" / "a8-genesis-provision.sh").is_file())

    def test_a_b3_genesis_that_funds_the_foreign_address_with_a_different_amount_fails(self):
        before = fx.genesis_before_b3()
        after = fx.genesis_after_b3(before)
        entry = next(e for e in after["app_state"]["bank"]["balances"] if e["address"] == harness.B3_FOREIGN_ADDRESS)
        entry["coins"][0]["amount"] = str(harness.B3_FOREIGN_AMOUNT + 1)

        def writer(path):
            fx.write_b3_provision(path, before=before, after=after, final=fx.genesis_final(fx.genesis_after_b3(before)))

        with self.assertRaisesRegex(a8.AcceptanceError, "B3_BALANCE_NOT_EXACT"):
            self.run_live(fx.ScriptedRunner(b3_writer=writer), scenario=self.B3)

    def test_a_b3_run_without_provisioner_evidence_fails_instead_of_passing_silently(self):
        with self.assertRaisesRegex(a8.AcceptanceError, "B3_GENESIS_EVIDENCE_MISSING"):
            self.run_live(fx.ScriptedRunner(), scenario=self.B3)

    def test_b3_is_refused_before_the_network_when_the_upstream_genesis_script_differs(self):
        with patch.object(harness, "GENESIS_PROVISIONER_UPSTREAM_SHA256", "0" * 64):
            runner = fx.ScriptedRunner()
            with self.assertRaises(a8.HarnessRefusal) as cm:
                self.run_live(runner, scenario=self.B3)
        self.assertEqual(cm.exception.code, "GENESIS_PROVISIONER_INCOMPATIBLE")
        self.assertFalse(any("--tests" in call for call in runner.calls))

    def test_a_non_b3_scenario_gets_no_genesis_provisioner(self):
        self.run_live(fx.ScriptedRunner())
        self.assertFalse((self.work_root / "network-root" / "local-test-net" / "a8-b3-genesis.yml").exists())
        self.assertFalse((self.evidence_dir / "genesis").exists())


class BuildExternalHarnessTests(SnapshotFixture):
    def test_a_build_writes_the_four_evidence_files_and_leaves_gonka_unchanged(self):
        runner = fx.ScriptedRunner()
        self.build(runner)
        evidence = self.work_root / "evidence"
        for rel in (
            "external-harness/api-compat.json",
            "external-harness/testermint-classpath.txt",
            "external-harness/harness-inputs.json",
            "source-immutability.json",
        ):
            self.assertTrue((evidence / rel).is_file(), rel)
        self.assertEqual(self.immutability(evidence)["verdict"], "UNCHANGED")
        self.assertEqual(
            [("a8ExportClasspath" in call, "testClasses" in call) for call in runner.calls],
            [(True, False), (False, True)],
        )
        self.assertFalse(any(call[0] in ("docker", "cargo") for call in runner.calls))

    def test_a_build_that_writes_into_gonka_fails_with_the_mutation_recorded(self):
        def mutate():
            (self.gonka / "testermint" / "build").mkdir()
            (self.gonka / "testermint" / "build" / "classes.jar").write_bytes(b"PK")

        with self.assertRaisesRegex(a8.AcceptanceError, "SOURCE_SNAPSHOT_MUTATED"):
            self.build(fx.ScriptedRunner(during_build=mutate))
        self.assertEqual(self.immutability(self.work_root / "evidence")["verdict"], "VIOLATED")

    def test_a_build_on_a_dirty_gonka_is_refused_before_gradle(self):
        (self.gonka / "testermint" / "gradlew.a8-linux").write_text("#!/bin/sh\n", "utf-8")
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.HarnessRefusal):
            self.build(runner)
        self.assertEqual(runner.calls, [])

    def test_a_build_against_an_incompatible_gonka_is_refused_with_the_missing_symbol(self):
        compose = self.gonka / "local-test-net" / "docker-compose-base.yml"
        compose.write_text(compose.read_text("utf-8").replace("${KEY_NAME}-api", "${KEY_NAME}-apiserver"), "utf-8")
        fx.git(self.gonka, "commit", "-qam", "rename api container")
        runner = fx.ScriptedRunner()
        with self.assertRaises(a8.HarnessRefusal) as cm:
            self.build(runner)
        self.assertEqual(cm.exception.code, "TESTERMINT_API_MISSING")
        self.assertIn("compose.api.container_name", str(cm.exception))
        self.assertEqual(runner.calls, [])

    def test_the_main_entry_point_returns_one_for_a_refused_build(self):
        (self.gonka / "stray.txt").write_text("x\n", "utf-8")
        argv = [
            "a8_acceptance.py", "build-external-harness",
            "--gonka-dir", str(self.gonka), "--work-root", str(self.work_root),
            "--testermint-harness-dir", str(self.harness_dir),
        ]
        with patch("sys.argv", argv), patch("sys.stderr"):
            self.assertEqual(a8.main(), 1)


class RemovedCliSurfaceTests(unittest.TestCase):
    BASE = ["run-live", "--gonka-dir", "gonka", "--expected-gonka-sha", "a" * 40]

    def test_the_overlay_and_prepared_tree_flags_no_longer_exist(self):
        for flag in (
            "--overlay-dir",
            "--expected-gonka-prepared-sha",
            "--expected-gonka-base-sha",
            "--allowed-test-path",
        ):
            with self.subTest(flag=flag), patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    a8.parser().parse_args([*self.BASE, flag, "x"])

    def test_run_live_without_an_explicit_gonka_sha_is_a_usage_error(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            a8.parser().parse_args(["run-live", "--gonka-dir", "gonka"])
        self.assertEqual(cm.exception.code, 2)

    def test_run_live_defaults_to_the_immutable_source_evidence_model(self):
        args = a8.parser().parse_args(self.BASE)
        self.assertEqual(args.evidence_model, a8.EVIDENCE_MODEL_IMMUTABLE)
        self.assertIsNone(args.work_root)

    def test_every_live_scenario_selector_maps_to_one_external_test(self):
        choices = next(
            action.choices
            for action in a8.parser()._subparsers._group_actions[0].choices["run-live"]._actions
            if action.dest == "scenario"
        )
        self.assertEqual(set(choices), set(a8.LIVE_SCENARIO_TESTS))
        self.assertEqual(len(a8.LIVE_SCENARIO_TESTS), 19)
        for test in a8.LIVE_SCENARIO_TESTS.values():
            self.assertTrue(test.startswith("MarketplaceContractAcceptanceTests."))


if __name__ == "__main__":
    unittest.main()
