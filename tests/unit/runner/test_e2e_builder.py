"""Unit tests for forward_e2e.execution.builder: recipe execution from unmodified source
snapshots, the source-snapshot write boundary, in-process staging, stale image
rejection and observed runtime verification.

The prepared-runtime tests (overlay applied and committed on top of the
selected commit) were removed together with ``prepare_runtime_tree``: that
behaviour no longer exists. Their immutable-model counterparts are here: the
selected sources are never written to, every output and staged context lives
under the build directory, and the running binary must report the selected
commit itself.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forward_e2e.execution.builder import (
    Builder,
    ImageObservation,
    parse_version_long,
    record_tool_versions,
    staged_tree_digest,
    verify_observed_runtime,
    verify_running_images,
)
from forward_e2e.execution.compat import (
    MOCK_SERVER_JAR_RELPATH,
    BuildStep,
    StageCopy,
    gonka_immutable_source_adapter,
    marketplace_contracts_adapter,
)
from forward_e2e.execution.errors import (
    BuildOutputInSourceError,
    BuildProvenanceError,
    ObservedVersionError,
    PathSafetyError,
)
from forward_e2e.execution.runlock import ManifestStatus
from tests.unit.runner.support.fakes import (
    FakeBuildRunner,
    completed,
    forbidden_runner,
    new_manifest,
    sha256_text,
    write_gonka_tree,
    write_text_file,
)

#: The selected Gonka commit. There is no prepared commit any more: the commit
#: that is built is the commit that was selected.
GONKA_SHA = "e86e4899bd8cf52d1ad4766c811f65230b2f9296"
#: Some other commit, for "the binary reports the wrong commit" negatives.
OTHER_GONKA_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
#: ``git describe --always`` of the selected commit, as executor.py measures it.
GONKA_VERSION = "v0.2.9-12-ge86e489"
CONTRACTS_SHA = "7497304e5dc6bf48accdd8c91549bc22de6997fc"
RUNNER_IMAGE_ID = "sha256:" + "1" * 64

BUILD_STARTED_EPOCH = 1_700_000_000.0

#: Every image the real Gonka recipe declares, in step order.
GONKA_RECIPE_IMAGES = [
    "ghcr.io/product-science/inferenced:latest",
    "ghcr.io/product-science/api:latest",
    "ghcr.io/product-science/edge-api:latest",
    "edge-api:latest",
    "ghcr.io/product-science/proxy:latest",
    "inference-mock-server:latest",
]


class BuilderTestCase(unittest.TestCase):
    """Two synthetic snapshots, a build directory and the executor's context."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-builder-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.gonka = write_gonka_tree(self.root / "gonka")
        # The inferenced build context stages cosmovisor/ next to
        # inference-chain/, exactly as upstream's DOCKER_BUILD does.
        write_text_file(self.gonka / "cosmovisor/cosmovisor-linux-amd64", "binary\n")
        self.contracts = self.root / "contracts"
        self.contracts.mkdir(parents=True)
        self.build_out = self.root / "build-out"
        self.build_out.mkdir(parents=True)
        self.log_dir = self.root / "logs"
        self.emitted = []
        # The same keys executor.py puts into the build context.
        self.context = {
            "runner": str(self.root / "runner"),
            "gonka": str(self.gonka),
            "contracts": str(self.contracts),
            "build_out": str(self.build_out),
            "platform": "linux/amd64",
            "goarch": "amd64",
            "gonka_sha": GONKA_SHA,
            "gonka_version": GONKA_VERSION,
            "contracts_sha": CONTRACTS_SHA,
            "runner_image_id": RUNNER_IMAGE_ID,
            "python": "/usr/local/bin/python3",
        }
        # executor.py offers the build directory as a working-directory role.
        self.roles = {"gonka": self.gonka, "contracts": self.contracts, "build_out": self.build_out}

    def tearDown(self):
        self.tmp_dir.cleanup()

    def produce_mock_server_jar(self, argv):
        """The Gradle wrapper writes the jar under the redirected build root."""
        if argv and argv[0] == "java":
            write_text_file(self.build_out / MOCK_SERVER_JAR_RELPATH, "jar\n")

    def run_gonka_recipe(self, runner, manifest=None, recipe=None):
        manifest = manifest if manifest is not None else new_manifest()
        adapter = gonka_immutable_source_adapter()
        Builder(runner=runner, log_dir=self.log_dir, emit=self.emitted.append).run_recipe(
            recipe if recipe is not None else adapter.build,
            manifest=manifest, context=self.context, roles=self.roles,
            started_epoch=BUILD_STARTED_EPOCH, adapter=adapter,
        )
        return manifest


class RecipeExecutionTests(BuilderTestCase):
    def produce_contract_outputs(self, argv):
        # Each process creates only its own outputs, so a skipped second step
        # cannot borrow artifacts from the first one.
        release_argv = [
            self.context["python"], f"{self.context['runner']}/vendor/contract_release/release.py", "build",
            "--repo", str(self.contracts), "--commit", CONTRACTS_SHA,
            "--output", str(self.build_out / "a9-release"),
        ]
        fixtures_argv = [
            "cargo", "build", "-p", "a8-caller", "-p", "a8-cw20",
            "--release", "--target", "wasm32-unknown-unknown",
            "--target-dir", str(self.build_out / "test-wasm-target"), "--locked",
        ]
        if list(argv) == release_argv:
            outputs = {
                "a9-release/build-manifest.json": "{}\n",
                "a9-release/wasm/marketplace_deal.wasm": "deal\n",
                "a9-release/wasm/marketplace_factory.wasm": "factory\n",
            }
        elif list(argv) == fixtures_argv:
            outputs = {
                "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm": "caller\n",
                "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm": "cw20\n",
            }
        else:
            self.fail(f"Unexpected build command: {argv!r}")
        for relative, content in outputs.items():
            write_text_file(self.build_out / relative, content)

    def test_a_timed_out_step_preserves_output_and_command_before_propagating_the_timeout(self):
        for stdout, stderr in (
            (b"compiler output\n", b"compiler error\xff\n"),
            ("compiler output\n", "compiler error\ufffd\n"),
            (None, None),
        ):
            with self.subTest(stdout=stdout):
                manifest = new_manifest()
                recipe = marketplace_contracts_adapter().build
                calls = []

                def timeout_runner(argv, **kwargs):
                    calls.append(argv)
                    raise subprocess.TimeoutExpired(
                        argv, kwargs["timeout"], output=stdout, stderr=stderr
                    )

                builder = Builder(runner=timeout_runner, log_dir=self.log_dir)
                with self.assertRaises(subprocess.TimeoutExpired):
                    builder.run_recipe(
                        recipe, manifest=manifest, context=self.context,
                        roles=self.roles, started_epoch=BUILD_STARTED_EPOCH,
                    )
                self.assertEqual(len(calls), 1, "later steps must not run")
                self.assertEqual(len(manifest.build_commands), 1)
                record = manifest.build_commands[0]
                self.assertEqual(record["argv"], calls[0])
                self.assertIsNone(record["exit_code"])
                self.assertTrue(record["completed_at_utc"])
                self.assertEqual(manifest.failures[0]["code"], "BUILD_STEP_TIMEOUT")
                self.assertEqual(
                    manifest.failures[0]["details"]["timeout_seconds"],
                    recipe.steps[0].timeout_seconds,
                )
                self.assertEqual(
                    (self.log_dir / "build-a9-release.log").read_text(encoding="utf-8"),
                    "compiler output\ncompiler error\ufffd\n" if stdout else "",
                )
                self.assertEqual(manifest.wasm, [])
                self.assertEqual(manifest.binaries, [])

    def test_a_declared_wasm_symlink_is_refused_even_when_its_target_is_inside_the_build(self):
        for outside in (False, True):
            with self.subTest(outside=outside):
                def produce_link(argv):
                    self.produce_contract_outputs(argv)
                    artifact = self.build_out / "a9-release/wasm/marketplace_deal.wasm"
                    target = (self.root if outside else self.build_out) / "linked-deal.wasm"
                    artifact.rename(target)
                    artifact.symlink_to(target)

                manifest = new_manifest()
                builder = Builder(runner=FakeBuildRunner(on_command=produce_link))
                with self.assertRaises(PathSafetyError):
                    builder.run_recipe(
                        marketplace_contracts_adapter().build, manifest=manifest,
                        context=self.context, roles=self.roles,
                        started_epoch=BUILD_STARTED_EPOCH,
                    )
                self.assertEqual(manifest.wasm, [])
                (self.build_out / "a9-release/wasm/marketplace_deal.wasm").unlink()

    def test_a_symlinked_parent_of_a_declared_wasm_is_refused_before_hashing_it(self):
        def produce_link(argv):
            self.produce_contract_outputs(argv)
            directory = self.build_out / "a9-release/wasm"
            target = self.build_out / "linked-wasm"
            directory.rename(target)
            directory.symlink_to(target, target_is_directory=True)

        manifest = new_manifest()
        builder = Builder(runner=FakeBuildRunner(on_command=produce_link))
        with self.assertRaises(PathSafetyError):
            builder.run_recipe(
                marketplace_contracts_adapter().build, manifest=manifest,
                context=self.context, roles=self.roles,
                started_epoch=BUILD_STARTED_EPOCH,
            )
        self.assertEqual(manifest.wasm, [])

    def test_contracts_build_argv_really_carries_the_selected_contracts_sha(self):
        runner = FakeBuildRunner(on_command=self.produce_contract_outputs)
        builder = Builder(runner=runner, log_dir=self.log_dir, emit=self.emitted.append)
        manifest = new_manifest()

        builder.run_recipe(
            marketplace_contracts_adapter().build,
            manifest=manifest,
            context=self.context,
            roles=self.roles,
            started_epoch=BUILD_STARTED_EPOCH,
        )

        release_argv = runner.calls[0]["argv"]
        self.assertIn(CONTRACTS_SHA, release_argv)
        self.assertEqual(release_argv[release_argv.index("--commit") + 1], CONTRACTS_SHA)
        self.assertEqual(release_argv[0], "/usr/local/bin/python3")
        self.assertIn(f"{self.root}/runner/vendor/contract_release/release.py", release_argv)
        self.assertEqual(release_argv[release_argv.index("--repo") + 1], str(self.contracts))
        self.assertNotIn(f"{self.contracts}/scripts/a9_release.py", release_argv)
        self.assertNotIn("{contracts_sha}", " ".join(release_argv))
        self.assertEqual(runner.calls[0]["cwd"], str(self.contracts))
        # And the manifest records the same resolved argv for review.
        self.assertEqual(manifest.build_commands[0]["argv"], release_argv)
        self.assertIn(CONTRACTS_SHA, manifest.build_commands[0]["argv"])
        self.assertEqual(manifest.build_commands[0]["exit_code"], 0)

    def test_build_outputs_are_hashed_and_split_between_wasm_and_binaries(self):
        runner = FakeBuildRunner(on_command=self.produce_contract_outputs)
        builder = Builder(runner=runner, log_dir=self.log_dir, emit=self.emitted.append)
        manifest = new_manifest()

        builder.run_recipe(
            marketplace_contracts_adapter().build,
            manifest=manifest,
            context=self.context,
            roles=self.roles,
            started_epoch=BUILD_STARTED_EPOCH,
        )

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(
            [command["step_id"] for command in manifest.build_commands],
            ["a9-release", "test-contracts"],
        )
        self.assertEqual(
            [command["argv"] for command in manifest.build_commands],
            [call["argv"] for call in runner.calls],
        )
        self.assertEqual(
            sorted(entry["role"] for entry in manifest.wasm),
            [
                "a9-release/wasm/marketplace_deal.wasm",
                "a9-release/wasm/marketplace_factory.wasm",
                "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm",
                "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm",
            ],
        )
        self.assertEqual(
            [entry["role"] for entry in manifest.binaries], ["a9-release/build-manifest.json"]
        )
        expected_contents = {
            "a9-release/build-manifest.json": "{}\n",
            "a9-release/wasm/marketplace_deal.wasm": "deal\n",
            "a9-release/wasm/marketplace_factory.wasm": "factory\n",
            "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm": "caller\n",
            "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm": "cw20\n",
        }
        self.assertEqual(
            {entry["role"]: entry["sha256"] for entry in manifest.wasm + manifest.binaries},
            {role: sha256_text(content) for role, content in expected_contents.items()},
        )
        manifest.finish(required_roles=["a9-release/build-manifest.json"])
        self.assertEqual(manifest.status, ManifestStatus.COMPLETE)
        self.assertTrue((self.log_dir / "build-a9-release.log").is_file())

    def test_every_chain_image_command_runs_in_the_build_directory_never_in_the_gonka_snapshot(self):
        runner = FakeBuildRunner(on_command=self.produce_mock_server_jar)

        manifest = self.run_gonka_recipe(runner)

        self.assertEqual(
            [call["argv"][:2] for call in runner.calls if call["argv"][0] == "docker"],
            [["docker", "build"]] * 5,
        )
        # The negative case: upstream `make build-docker` would have run in the
        # checkout. Nothing runs there now.
        self.assertEqual({call["cwd"] for call in runner.calls}, {str(self.build_out)})
        docker_calls = [call for call in runner.calls if call["argv"][0] == "docker"]
        self.assertTrue(all(call["env"]["DOCKER_BUILDKIT"] == "1" for call in docker_calls))
        self.assertEqual([entry["role"] for entry in manifest.images], GONKA_RECIPE_IMAGES)
        self.assertEqual(
            manifest.images[0]["image_id"],
            runner.image_id_for("ghcr.io/product-science/inferenced:latest"),
        )
        self.assertTrue(any("[build] inferenced-image" in line for line in self.emitted))

    def test_the_full_chain_recipe_issues_no_git_command_and_leaves_both_snapshots_byte_identical(self):
        # Immutable-model counterpart of the removed "nothing was committed"
        # assertion: no git process at all, and both snapshots unchanged.
        before = {role: staged_tree_digest(self.roles[role]) for role in ("gonka", "contracts")}
        runner = FakeBuildRunner(on_command=self.produce_mock_server_jar)

        self.run_gonka_recipe(runner)

        self.assertEqual([c["argv"] for c in runner.calls if c["argv"][0] == "git"], [])
        self.assertEqual(
            {role: staged_tree_digest(self.roles[role]) for role in ("gonka", "contracts")},
            before,
        )
        self.assertFalse((self.gonka / "inference-chain/.docker-context").exists())

    def test_placeholders_resolve_to_the_selected_shas_and_paths(self):
        builder = Builder(runner=FakeBuildRunner())

        resolved = builder.substitute(
            "{python} {gonka} {contracts} {build_out} {platform} "
            "{gonka_sha} {gonka_version} {contracts_sha}",
            self.context,
        )

        self.assertEqual(
            resolved,
            f"/usr/local/bin/python3 {self.gonka} {self.contracts} {self.build_out} "
            f"linux/amd64 {GONKA_SHA} {GONKA_VERSION} {CONTRACTS_SHA}",
        )
        self.assertNotIn("{", resolved)

    def test_stale_cached_image_is_refused_and_recorded_as_stale_image_reused(self):
        runner = FakeBuildRunner(image_created_epoch=BUILD_STARTED_EPOCH - 3600.0)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.run_gonka_recipe(runner, manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertIn(
            "is not recorded as having been produced from the selected sources",
            str(cm.exception),
        )
        self.assertEqual(cm.exception.code, "BUILD_PROVENANCE_FAILED")
        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "STALE_IMAGE_REUSED")
        self.assertEqual(failure["details"]["reason"], "no record of this image id")
        self.assertEqual(failure["details"]["build_started_epoch"], BUILD_STARTED_EPOCH)
        self.assertLess(failure["details"]["image_created_epoch"], BUILD_STARTED_EPOCH)
        self.assertEqual(manifest.images, [])
        manifest.finish()
        self.assertEqual(manifest.status, ManifestStatus.FAILED)
        self.assertNotEqual(manifest.status, ManifestStatus.COMPLETE)

    def test_declared_image_that_was_not_produced_is_reported_as_missing(self):
        runner = FakeBuildRunner(missing_images={"ghcr.io/product-science/proxy:latest"})
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.run_gonka_recipe(runner, manifest)

        self.assertIn("A declared image is missing after its build step", str(cm.exception))
        self.assertEqual(manifest.failures[-1]["code"], "BUILD_IMAGE_MISSING")
        self.assertEqual(
            manifest.failures[-1]["details"]["reference"],
            "ghcr.io/product-science/proxy:latest",
        )

    def test_failed_build_step_records_the_failure_and_never_completes(self):
        runner = FakeBuildRunner(failing_steps=("release.py",),)
        builder = Builder(runner=runner, log_dir=self.log_dir)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            builder.run_recipe(
                marketplace_contracts_adapter().build,
                manifest=manifest,
                context=self.context,
                roles=self.roles,
                started_epoch=BUILD_STARTED_EPOCH,
            )

        self.assertIn("'a9-release' failed", str(cm.exception))
        self.assertIn("no artefacts are recorded as complete", str(cm.exception))
        self.assertEqual(manifest.failures[0]["code"], "BUILD_STEP_FAILED")
        self.assertEqual(manifest.failures[0]["details"]["step_id"], "a9-release")
        self.assertIn("compilation failed", manifest.failures[0]["details"]["stderr"])
        self.assertEqual(manifest.status, ManifestStatus.FAILED)
        self.assertEqual(manifest.binaries, [])
        self.assertEqual(manifest.wasm, [])
        # Only the first step ran; the second was never attempted.
        self.assertEqual(len(manifest.build_commands), 1)
        manifest.finish(required_roles=["a9-release/build-manifest.json"])
        self.assertNotEqual(manifest.status, ManifestStatus.COMPLETE)

    def test_build_diagnostics_redact_credentials_from_manifest_and_log(self):
        secret = "ghp_" + "A" * 32

        class SecretFailingRunner(FakeBuildRunner):
            def __call__(self, argv, *, cwd=None, env=None, timeout=None):
                if "release.py" in " ".join(map(str, argv)):
                    return completed(argv, returncode=2, stderr=f"remote rejected {secret}")
                return super().__call__(argv, cwd=cwd, env=env, timeout=timeout)

        manifest = new_manifest()
        builder = Builder(runner=SecretFailingRunner(), log_dir=self.log_dir)
        with self.assertRaises(BuildProvenanceError):
            builder.run_recipe(
                marketplace_contracts_adapter().build, manifest=manifest,
                context=self.context, roles=self.roles, started_epoch=BUILD_STARTED_EPOCH,
            )

        self.assertNotIn(secret, str(manifest.to_dict()))
        self.assertIn("***REDACTED***", manifest.failures[0]["details"]["stderr"])
        log = (self.log_dir / "build-a9-release.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log)
        self.assertIn("***REDACTED***", log)

    def test_each_missing_declared_build_output_identifies_the_artifact_and_step(self):
        recipe = marketplace_contracts_adapter().build
        cases = [(step.step_id, relative) for step in recipe.steps for relative in step.produces_files]
        for index, (step_id, relative) in enumerate(cases):
            with self.subTest(missing=relative):
                # Isolate outputs so previous cases cannot supply a missing file.
                self.build_out = self.root / f"missing-output-{index}"
                self.build_out.mkdir()
                self.context["build_out"] = str(self.build_out)
                missing_path = self.build_out / relative

                def produce_except_one(argv):
                    self.produce_contract_outputs(argv)
                    if missing_path.is_file():
                        missing_path.unlink()

                runner = FakeBuildRunner(on_command=produce_except_one)
                builder = Builder(runner=runner)
                manifest = new_manifest()

                with self.assertRaises(BuildProvenanceError) as cm:
                    builder.run_recipe(
                        recipe, manifest=manifest, context=self.context,
                        roles=self.roles, started_epoch=BUILD_STARTED_EPOCH,
                    )

                self.assertIn("A declared build output is missing", str(cm.exception))
                expected_details = {"step_id": step_id, "path": str(missing_path)}
                self.assertEqual(cm.exception.details, expected_details)
                self.assertEqual(manifest.failures[-1]["code"], "BUILD_FILE_MISSING")
                self.assertEqual(manifest.failures[-1]["details"], expected_details)
                self.assertEqual(manifest.status, ManifestStatus.FAILED)

    def test_unknown_working_directory_role_is_refused(self):
        runner = FakeBuildRunner(on_command=self.produce_contract_outputs)
        builder = Builder(runner=runner)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            builder.run_recipe(
                marketplace_contracts_adapter().build,
                manifest=manifest,
                context=self.context,
                roles={"gonka": self.gonka, "build_out": self.build_out},
                started_epoch=BUILD_STARTED_EPOCH,
            )

        self.assertIn("unknown working directory role", str(cm.exception))
        self.assertEqual(cm.exception.details["cwd_role"], "contracts")

    def test_fresh_image_observation_is_parsed_from_docker_inspect(self):
        runner = FakeBuildRunner()
        builder = Builder(runner=runner)

        observation = builder.inspect_image("ghcr.io/product-science/api:latest")

        self.assertIsInstance(observation, ImageObservation)
        self.assertEqual(
            observation.image_id, runner.image_id_for("ghcr.io/product-science/api:latest")
        )
        self.assertAlmostEqual(
            observation.created_epoch, BUILD_STARTED_EPOCH + 60.0, places=3
        )

    def test_inspect_of_an_absent_image_returns_none_instead_of_raising(self):
        runner = FakeBuildRunner(missing_images={"nope:latest"})
        builder = Builder(runner=runner)

        self.assertIsNone(builder.inspect_image("nope:latest"))

    def test_tool_versions_are_recorded_from_the_injected_runner(self):
        manifest = new_manifest()

        def probe_runner(argv, *, cwd=None, env=None, timeout=None):
            return completed(argv, stdout="git version 2.44.0")

        record_tool_versions(manifest, runner=probe_runner, probes=(("git", ("git", "--version")),))

        self.assertEqual(manifest.tools[0]["name"], "git")
        self.assertEqual(manifest.tools[0]["version"], "git version 2.44.0")
        self.assertEqual(manifest.tools[0]["exit_code"], 0)

    def test_tool_version_output_redacts_credential_like_values(self):
        manifest = new_manifest()
        secret = "ghp_" + "B" * 32

        def probe_runner(argv, *, cwd=None, env=None, timeout=None):
            return completed(argv, stdout=f"tool version {secret}")

        record_tool_versions(manifest, runner=probe_runner, probes=(("tool", ("tool", "--version")),))
        self.assertNotIn(secret, str(manifest.tools))
        self.assertIn("***REDACTED***", manifest.tools[0]["version"])


class SourceSnapshotWriteBoundaryTests(BuilderTestCase):
    """A step that would write into a snapshot is refused before it runs.

    Every negative below is ``self.valid_step`` (or ``self.valid_stage``) with
    exactly one output location moved; the positive controls prove that the
    unmodified steps are accepted, so a refusal can only come from that move.
    """

    def setUp(self):
        super().setUp()
        self.valid_step = BuildStep(
            step_id="synthetic-out-of-tree-build",
            argv=(
                "tool", "--project-cache-dir", "{build_out}/project-cache",
                "-Pa8.outRoot={build_out}/out", "build",
            ),
            cwd_role="build_out", timeout_seconds=60,
            description="Synthetic build whose every output is redirected",
            env={"GRADLE_USER_HOME": "{build_out}/gradle-home"},
            produces_files=("out/artifact.bin",),
        )
        self.valid_stage = BuildStep(
            step_id="synthetic-stage", argv=(), cwd_role="build_out",
            timeout_seconds=60, description="Synthetic staging step",
            stage=(StageCopy(source="{gonka}/inference-chain",
                             destination="{build_out}/docker-context/chain"),),
        )

    def produce_artifact(self, argv):
        write_text_file(self.build_out / "out/artifact.bin", "artifact\n")

    def run_steps(self, runner, *steps):
        recipe = dataclasses.replace(
            gonka_immutable_source_adapter().build, steps=tuple(steps)
        )
        return self.run_gonka_recipe(runner, recipe=recipe)

    def assert_refused_before_anything_runs(self, step, *, what, path, source_role):
        runner = FakeBuildRunner(on_command=self.produce_artifact)
        manifest = new_manifest()
        before = staged_tree_digest(self.roles[source_role]) if source_role in self.roles else None

        with self.assertRaises(BuildOutputInSourceError) as cm:
            self.run_gonka_recipe(
                runner, manifest,
                recipe=dataclasses.replace(
                    gonka_immutable_source_adapter().build, steps=(step,)
                ),
            )

        offenders = [{"what": what, "path": str(path), "source_role": source_role}]
        self.assertEqual(cm.exception.code, "BUILD_OUTPUT_IN_SOURCE")
        self.assertEqual(cm.exception.details["offenders"], offenders)
        self.assertEqual(manifest.failures[-1]["code"], "BUILD_OUTPUT_IN_SOURCE")
        self.assertEqual(manifest.failures[-1]["details"]["offenders"], offenders)
        # Nothing executed: no process, no command record, nothing staged.
        self.assertEqual(runner.calls, [])
        self.assertEqual(manifest.build_commands, [])
        self.assertEqual(manifest.staged_contexts, [])
        self.assertFalse(Path(path).exists())
        if before is not None:
            self.assertEqual(staged_tree_digest(self.roles[source_role]), before)

    def test_the_valid_redirected_step_and_the_valid_stage_step_are_accepted(self):
        runner = FakeBuildRunner(on_command=self.produce_artifact)

        manifest = self.run_steps(runner, self.valid_stage, self.valid_step)

        self.assertEqual(manifest.failures, [])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([r["step_id"] for r in manifest.build_commands],
                         ["synthetic-stage", "synthetic-out-of-tree-build"])

    def test_a_declared_output_inside_either_snapshot_is_refused(self):
        for role in ("gonka", "contracts"):
            with self.subTest(role=role):
                step = dataclasses.replace(
                    self.valid_step, produces_files=("{" + role + "}/out/artifact.bin",)
                )
                self.assert_refused_before_anything_runs(
                    step, what="declared output",
                    path=self.roles[role] / "out/artifact.bin", source_role=role,
                )

    def test_a_project_cache_dir_inside_either_snapshot_is_refused(self):
        for role in ("gonka", "contracts"):
            with self.subTest(role=role):
                argv = list(self.valid_step.argv)
                argv[2] = "{" + role + "}/.gradle"
                self.assert_refused_before_anything_runs(
                    dataclasses.replace(self.valid_step, argv=tuple(argv)),
                    what="--project-cache-dir", path=self.roles[role] / ".gradle",
                    source_role=role,
                )

    def test_an_a8_out_root_property_inside_either_snapshot_is_refused(self):
        for role in ("gonka", "contracts"):
            with self.subTest(role=role):
                argv = list(self.valid_step.argv)
                argv[3] = "-Pa8.outRoot={" + role + "}/build"
                self.assert_refused_before_anything_runs(
                    dataclasses.replace(self.valid_step, argv=tuple(argv)),
                    what="-Pa8.outRoot", path=self.roles[role] / "build", source_role=role,
                )

    def test_a_gradle_user_home_inside_either_snapshot_is_refused(self):
        for role in ("gonka", "contracts"):
            with self.subTest(role=role):
                step = dataclasses.replace(
                    self.valid_step, env={"GRADLE_USER_HOME": "{" + role + "}/.gradle-home"}
                )
                self.assert_refused_before_anything_runs(
                    step, what="GRADLE_USER_HOME",
                    path=self.roles[role] / ".gradle-home", source_role=role,
                )

    def test_a_stage_destination_inside_either_snapshot_is_refused(self):
        for role in ("gonka", "contracts"):
            with self.subTest(role=role):
                # Exactly upstream's DOCKER_BUILD location, which is why the
                # runner stages the context itself.
                destination = "{" + role + "}/inference-chain/.docker-context"
                step = dataclasses.replace(self.valid_stage, stage=(dataclasses.replace(
                    self.valid_stage.stage[0], destination=destination),))
                runner_path = self.roles[role] / "inference-chain/.docker-context"
                with self.assertRaises(BuildOutputInSourceError) as cm:
                    self.run_steps(FakeBuildRunner(), step)
                offenders = cm.exception.details["offenders"]
                # Inside a snapshot is necessarily outside build_out as well;
                # both facts are reported, the snapshot one first.
                self.assertEqual(offenders[0], {"what": "stage destination",
                                                "path": str(runner_path), "source_role": role})
                self.assertEqual(offenders[1]["source_role"], "outside build_out")
                self.assertFalse(runner_path.exists())

    def test_a_stage_destination_outside_the_build_directory_is_refused(self):
        elsewhere = self.root / "elsewhere/chain"
        step = dataclasses.replace(self.valid_stage, stage=(dataclasses.replace(
            self.valid_stage.stage[0], destination=str(elsewhere)),))

        self.assert_refused_before_anything_runs(
            step, what="stage destination", path=elsewhere, source_role="outside build_out",
        )


class InProcessStagingTests(BuilderTestCase):
    """The real ``stage-inferenced-context`` step, run by the builder itself."""

    def setUp(self):
        super().setUp()
        recipe = gonka_immutable_source_adapter().build
        self.stage_step = next(s for s in recipe.steps if s.step_id == "stage-inferenced-context")
        self.recipe = dataclasses.replace(recipe, steps=(self.stage_step,))

    def stage(self, manifest=None):
        # A stage step never starts a process: forbidden_runner proves it.
        return self.run_gonka_recipe(forbidden_runner, manifest, recipe=self.recipe)

    def test_each_staged_context_records_the_digest_of_its_source_and_lands_under_the_build_directory(self):
        sources = [self.gonka / "inference-chain", self.gonka / "cosmovisor"]
        expected = [staged_tree_digest(source) for source in sources]

        manifest = self.stage()

        self.assertEqual([entry["source"] for entry in manifest.staged_contexts],
                         [str(source) for source in sources])
        self.assertEqual([entry["digest"] for entry in manifest.staged_contexts], expected)
        for entry in manifest.staged_contexts:
            destination = Path(entry["destination"])
            self.assertEqual(staged_tree_digest(destination), entry["digest"])
            self.assertTrue(destination.is_relative_to(self.build_out / "docker-context/inferenced"))
        record = manifest.build_commands[0]
        self.assertEqual((record["argv"], record["exit_code"]), ([], 0))
        self.assertEqual(record["staged"], manifest.staged_contexts)
        # The source is read, never modified.
        self.assertEqual([staged_tree_digest(source) for source in sources], expected)

    def test_a_symlink_inside_the_staged_source_is_copied_as_a_link_and_never_followed(self):
        (self.gonka / "inference-chain/linked").symlink_to("../outside-the-tree")

        manifest = self.stage()

        staged = Path(manifest.staged_contexts[0]["destination"]) / "linked"
        self.assertTrue(staged.is_symlink())
        self.assertEqual(staged.readlink(), Path("../outside-the-tree"))
        self.assertEqual(manifest.staged_contexts[0]["digest"],
                         staged_tree_digest(self.gonka / "inference-chain"))

    def test_a_missing_stage_source_is_refused_as_build_stage_source_missing(self):
        shutil.rmtree(self.gonka / "cosmovisor")
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.stage(manifest)

        self.assertEqual(manifest.failures[-1]["code"], "BUILD_STAGE_SOURCE_MISSING")
        self.assertEqual(cm.exception.details["source"], str(self.gonka / "cosmovisor"))
        self.assertEqual(manifest.staged_contexts, [])

    def test_a_stage_source_that_is_a_symlink_is_refused_instead_of_followed(self):
        real = self.root / "cosmovisor-elsewhere"
        (self.gonka / "cosmovisor").rename(real)
        (self.gonka / "cosmovisor").symlink_to(real, target_is_directory=True)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError):
            self.stage(manifest)

        self.assertEqual(manifest.failures[-1]["code"], "BUILD_STAGE_SOURCE_MISSING")

    def test_a_staged_copy_that_differs_from_its_source_is_refused_as_build_stage_mismatch(self):
        real_copytree = shutil.copytree

        def tampering_copytree(source, destination, *args, **kwargs):
            # The filesystem boundary misbehaves: one staged byte changes.
            result = real_copytree(source, destination, *args, **kwargs)
            go_mod = Path(destination) / "go.mod"
            if go_mod.is_file():
                with open(go_mod, "a", encoding="utf-8") as handle:
                    handle.write("// tampered\n")
            return result

        manifest = new_manifest()
        with patch("forward_e2e.execution.builder.shutil.copytree", side_effect=tampering_copytree):
            with self.assertRaises(BuildProvenanceError):
                self.stage(manifest)

        self.assertEqual(manifest.failures[-1]["code"], "BUILD_STAGE_MISMATCH")
        self.assertEqual(manifest.staged_contexts, [])

    def test_a_missing_empty_directory_in_staged_context_is_refused(self):
        (self.gonka / "inference-chain/empty-build-input").mkdir()
        real_copytree = shutil.copytree

        def omitting_empty_directory(source, destination, *args, **kwargs):
            result = real_copytree(source, destination, *args, **kwargs)
            if Path(source) == self.gonka / "inference-chain":
                (Path(destination) / "empty-build-input").rmdir()
            return result

        manifest = new_manifest()
        with patch("forward_e2e.execution.builder.shutil.copytree", side_effect=omitting_empty_directory):
            with self.assertRaises(BuildProvenanceError):
                self.stage(manifest)

        self.assertEqual(manifest.failures[-1]["code"], "BUILD_STAGE_MISMATCH")


class ImmutableSourceRecipeTests(BuilderTestCase):
    """Static and executed facts about the real ``gonka_immutable_source_adapter`` recipe."""

    OUTPUT_FLAGS = ("-o", "--output", "--project-cache-dir", "--target-dir", "--out-dir", "--build-dir")
    OUTPUT_PROPERTIES = ("-Pa8.outRoot=", "-Pkotlin.project.persistent.dir=",
                         "-Dkotlin.project.persistent.dir=")
    OUTPUT_ENV = ("GRADLE_USER_HOME", "CARGO_TARGET_DIR", "GOCACHE", "GOMODCACHE")

    def setUp(self):
        super().setUp()
        self.recipe = gonka_immutable_source_adapter().build

    def declared_output_templates(self, step):
        """Read straight from the recipe data, independently of the builder."""
        found = [("stage destination", copy.destination) for copy in step.stage]
        found += [("declared output", "{build_out}/" + rel) for rel in step.produces_files]
        for index, token in enumerate(step.argv):
            if token in self.OUTPUT_FLAGS:
                found.append((token, step.argv[index + 1]))
            found += [(p, token[len(p):]) for p in self.OUTPUT_PROPERTIES if token.startswith(p)]
        found += [(k, v) for k, v in dict(step.env).items() if k in self.OUTPUT_ENV]
        return found

    def test_the_recipe_has_the_expected_steps_and_none_runs_inside_a_snapshot(self):
        self.assertEqual([step.step_id for step in self.recipe.steps], [
            "stage-inferenced-context", "inferenced-image", "api-image", "edge-api-image",
            "proxy-image", "mock-server-jar", "stage-mock-server-context", "mock-server-image",
        ])
        self.assertEqual({step.cwd_role for step in self.recipe.steps}, {"build_out"})

    def test_every_output_cache_and_staged_context_of_the_recipe_is_under_the_build_directory(self):
        seen = set()
        for step in self.recipe.steps:
            for label, template in self.declared_output_templates(step):
                with self.subTest(step=step.step_id, what=label):
                    seen.add(label)
                    self.assertTrue(template.startswith("{build_out}/"), template)
            # And the builder's own boundary check accepts the step.
            Builder(runner=forbidden_runner).assert_outputs_outside_sources(
                step, manifest=new_manifest(), context=self.context, roles=self.roles
            )
        # The negative case: the scan really found the redirections it guards.
        self.assertTrue({"stage destination", "declared output", "--project-cache-dir",
                         "-Pa8.outRoot=", "GRADLE_USER_HOME"} <= seen, seen)

    def test_the_mock_server_jar_is_built_with_the_wrapper_jar_and_never_the_gradlew_script(self):
        step = next(s for s in self.recipe.steps if s.step_id == "mock-server-jar")
        argv = list(step.argv)

        self.assertEqual(argv[0], "java")
        self.assertEqual(
            argv[argv.index("-cp") + 1],
            "{gonka}/testermint/mock_server/gradle/wrapper/gradle-wrapper.jar",
        )
        self.assertEqual(argv[argv.index("-cp") + 2], "org.gradle.wrapper.GradleWrapperMain")
        self.assertTrue(argv[argv.index("--init-script") + 1].startswith("{runner}/harness/"))
        for step_ in self.recipe.steps:
            for token in step_.argv:
                self.assertNotIn("gradlew", token)
                self.assertNotEqual(token, "make")

    def test_every_command_records_its_resolved_build_args_and_passes_each_as_a_build_arg(self):
        # A platform other than amd64 separates the api image's upstream pin
        # (always linux/amd64) from the locked platform of the other images.
        self.context.update(platform="linux/arm64", goarch="arm64")
        builder = Builder(runner=forbidden_runner)
        manifest = self.run_gonka_recipe(FakeBuildRunner(on_command=self.produce_mock_server_jar))

        records = {record["step_id"]: record for record in manifest.build_commands}
        self.assertEqual(list(records), [step.step_id for step in self.recipe.steps])
        for step in self.recipe.steps:
            with self.subTest(step=step.step_id):
                expected = {k: builder.substitute(v, self.context) for k, v in step.build_args}
                self.assertEqual(records[step.step_id]["build_args"], expected)
                argv = records[step.step_id]["argv"]
                passed = [argv[i + 1] for i, token in enumerate(argv) if token == "--build-arg"]
                self.assertEqual(passed, [f"{k}={v}" for k, v in expected.items()])
        inferenced = records["inferenced-image"]["build_args"]
        self.assertIn(f"version.Commit={GONKA_SHA}", inferenced["LDFLAGS"])
        self.assertIn(f"version.Version={GONKA_VERSION}", inferenced["LDFLAGS"])
        self.assertEqual(inferenced["GOARCH"], "arm64")
        self.assertEqual(records["api-image"]["build_args"]["GOARCH"], "amd64")
        self.assertEqual(records["api-image"]["build_args"]["DEVSHARD_VERSION"], GONKA_VERSION)
        self.assertEqual(records["proxy-image"]["build_args"], {})
        self.assertEqual(records["stage-inferenced-context"]["build_args"], {})


class ObservedRuntimeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-builder-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.sources = write_gonka_tree(self.root / "sources")
        self.adapter = gonka_immutable_source_adapter()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def verify(self, observed):
        return verify_observed_runtime(
            self.adapter, observed, sources_root=self.sources, selected_sha=GONKA_SHA,
        )

    def test_matching_runtime_returns_one_record_per_expectation(self):
        results = self.verify(
            {"gonka_source_sha": GONKA_SHA, "wasmd": "v0.53.0", "wasmvm": "v2.1.2"}
        )

        self.assertEqual([r["field"] for r in results], ["gonka_source_sha", "wasmd", "wasmvm"])
        self.assertEqual(results[0]["expected"], GONKA_SHA)
        self.assertEqual(results[0]["kind"], "selected_commit")
        self.assertEqual(results[0]["source_of_truth"], "selected commit")
        self.assertEqual(results[1]["expected"], "v0.53.0")
        self.assertEqual(
            results[1]["source_of_truth"], "inference-chain/go.mod:github.com/CosmWasm/wasmd"
        )

    def test_the_selected_commit_is_compared_case_insensitively_as_git_prints_lowercase(self):
        results = self.verify(
            {"gonka_source_sha": GONKA_SHA.upper(), "wasmd": "v0.53.0", "wasmvm": "v2.1.2"}
        )

        self.assertEqual(results[0]["actual"], GONKA_SHA.upper())

    def test_wrong_wasmd_version_is_rejected_with_expected_and_actual(self):
        with self.assertRaises(ObservedVersionError) as cm:
            self.verify(
                {"gonka_source_sha": GONKA_SHA, "wasmd": "v0.51.0", "wasmvm": "v2.1.2"}
            )

        message = str(cm.exception)
        self.assertIn("Observed runtime version does not match the selected sources", message)
        self.assertIn("v0.51.0", message)
        self.assertIn("v0.53.0", message)
        self.assertEqual(cm.exception.code, "OBSERVED_VERSION_MISMATCH")
        self.assertEqual(cm.exception.details["field"], "wasmd")

    def test_wrong_wasmvm_version_is_rejected(self):
        with self.assertRaises(ObservedVersionError) as cm:
            self.verify(
                {"gonka_source_sha": GONKA_SHA, "wasmd": "v0.53.0", "wasmvm": "v2.0.0"}
            )

        self.assertEqual(cm.exception.details["field"], "wasmvm")
        self.assertEqual(cm.exception.details["expected"], "v2.1.2")
        self.assertEqual(cm.exception.details["actual"], "v2.0.0")

    def test_binary_reporting_any_commit_other_than_the_selected_one_is_explained(self):
        # Replaces the removed "reports the unpatched requested commit" case:
        # with nothing patched, every other commit means another binary runs.
        with self.assertRaises(ObservedVersionError) as cm:
            self.verify(
                {"gonka_source_sha": OTHER_GONKA_SHA, "wasmd": "v0.53.0", "wasmvm": "v2.1.2"}
            )

        message = str(cm.exception)
        self.assertIn("does not report the selected commit", message)
        self.assertIn("cached or externally supplied image", message)
        self.assertEqual(cm.exception.details["expected"], GONKA_SHA)
        self.assertEqual(cm.exception.details["actual"], OTHER_GONKA_SHA)

    def test_component_that_reports_nothing_cannot_be_verified(self):
        with self.assertRaises(ObservedVersionError) as cm:
            self.verify({"gonka_source_sha": GONKA_SHA, "wasmd": "v0.53.0"})

        self.assertIn(
            "The running component did not report the field required for verification",
            str(cm.exception),
        )

    def test_a_binary_that_reports_no_commit_cannot_be_tied_to_the_selected_commit(self):
        with self.assertRaises(ObservedVersionError) as cm:
            self.verify({"wasmd": "v0.53.0", "wasmvm": "v2.1.2"})

        self.assertEqual(cm.exception.details["field"], "gonka_source_sha")
        self.assertIsNone(cm.exception.details["actual"])

    def test_version_that_cannot_be_measured_from_the_sources_fails_closed(self):
        go_mod = self.sources / "inference-chain/go.mod"
        original = go_mod.read_text(encoding="utf-8")
        for field, module in (
            ("wasmd", "github.com/CosmWasm/wasmd"),
            ("wasmvm", "github.com/CosmWasm/wasmvm/v2"),
        ):
            with self.subTest(missing_dependency=field):
                # Start from the same valid fixture each time and remove only
                # the dependency whose missing expectation this case proves.
                lines = original.splitlines(keepends=True)
                removed = [line for line in lines if line.strip().startswith(module + " ")]
                self.assertEqual(len(removed), 1)
                write_text_file(go_mod, original.replace(removed[0], "", 1))
                with self.assertRaises(ObservedVersionError) as cm:
                    self.verify(
                        {"gonka_source_sha": GONKA_SHA, "wasmd": "v0.53.0", "wasmvm": "v2.1.2"}
                    )
                self.assertIn(
                    "The expected runtime version could not be measured from the selected sources",
                    str(cm.exception),
                )
                self.assertEqual(cm.exception.details["field"], field)
                self.assertIsNone(cm.exception.details["expected"])

    def test_version_long_output_is_parsed_into_the_fields_under_verification(self):
        text = (
            "name: inferenced\n"
            "server_name: inferenced\n"
            f"commit: {GONKA_SHA}\n"
            "cosmos_sdk_version: v0.53.0\n"
            "go: go version go1.23.6 darwin/arm64\n"
            "build_deps:\n"
            "- github.com/CosmWasm/wasmd@v0.53.0\n"
            "- github.com/CosmWasm/wasmvm/v2@v2.1.2\n"
        )

        parsed = parse_version_long(text)

        self.assertEqual(parsed["gonka_source_sha"], GONKA_SHA)
        self.assertEqual(parsed["wasmd"], "v0.53.0")
        self.assertEqual(parsed["wasmvm"], "v2.1.2")
        self.assertEqual(parsed["cosmos_sdk_version"], "v0.53.0")


class RunningImageProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.node_image_id = "sha256:" + "a" * 64
        self.api_image_id = "sha256:" + "b" * 64
        self.foreign_image_id = "sha256:" + "f" * 64
        self.built = [
            {"role": "ghcr.io/product-science/inferenced:latest", "image_id": self.node_image_id},
            {"role": "ghcr.io/product-science/api:latest", "image_id": self.api_image_id},
        ]
        # prepare_runtime_dependencies records the registry index pin and the
        # local platform image ID separately; they identify different objects.
        self.postgres_digest = "sha256:" + "c" * 64
        self.postgres_image_id = "sha256:" + "d" * 64
        reference = "postgres:18.1-bookworm"
        pinned = f"{reference}@{self.postgres_digest}"
        self.external_case = {
            "expected_images": self.built,
            "running": {"genesis-postgres": {
                "image": self.postgres_image_id, "image_reference": reference,
            }},
            "runtime_external_images": {"postgres": pinned},
            "runtime_dependencies": [{
                "reference": reference, "pinned": pinned,
                "platform": "linux/amd64", "image_id": self.postgres_image_id,
                "repo_digests": [f"postgres@{self.postgres_digest}"],
            }],
            "platform": "linux/amd64",
        }

    def test_containers_running_the_built_images_are_accepted(self):
        findings = verify_running_images(
            expected_images=self.built,
            running={"genesis-node": self.node_image_id, "join1-api": self.api_image_id},
        )

        self.assertEqual([f["from_this_build"] for f in findings], [True, True])
        self.assertEqual(
            sorted(f["container"] for f in findings), ["genesis-node", "join1-api"]
        )

    def test_container_running_another_image_id_is_a_provenance_failure(self):
        with self.assertRaises(BuildProvenanceError) as cm:
            verify_running_images(
                expected_images=self.built,
                running={"genesis-node": self.node_image_id, "join1-api": self.foreign_image_id},
            )

        message = str(cm.exception)
        self.assertIn(
            "Containers are running images that this build did not produce", message
        )
        self.assertIn("The selected sources are therefore not what is being tested", message)
        self.assertEqual(cm.exception.code, "BUILD_PROVENANCE_FAILED")
        self.assertEqual(cm.exception.details["offenders"], {"join1-api": self.foreign_image_id})
        self.assertEqual(
            cm.exception.details["built_image_ids"], [self.node_image_id, self.api_image_id]
        )

    def test_empty_build_manifest_makes_every_running_container_an_offender(self):
        with self.assertRaises(BuildProvenanceError) as cm:
            verify_running_images(
                expected_images=[], running={"genesis-node": self.node_image_id}
            )

        self.assertEqual(cm.exception.details["built_image_ids"], [])
        self.assertIn("genesis-node", cm.exception.details["offenders"])

    def test_only_an_exactly_pinned_external_runtime_image_is_accepted(self):
        findings = verify_running_images(**self.external_case)

        self.assertFalse(findings[0]["from_this_build"])
        self.assertTrue(findings[0]["pinned_runtime_dependency"])

    def test_external_runtime_tag_without_the_pinned_image_id_is_rejected(self):
        unexpected_id = "sha256:" + "e" * 64
        self.external_case["running"]["genesis-postgres"]["image"] = unexpected_id
        with self.assertRaises(BuildProvenanceError) as cm:
            verify_running_images(**self.external_case)

        self.assertEqual(cm.exception.details["offenders"], {"genesis-postgres": unexpected_id})

    def test_a_registry_digest_cannot_stand_in_for_the_running_platform_image_id(self):
        self.external_case["running"]["genesis-postgres"]["image"] = self.postgres_digest
        with self.assertRaises(BuildProvenanceError) as cm:
            verify_running_images(**self.external_case)
        self.assertEqual(
            cm.exception.details["offenders"], {"genesis-postgres": self.postgres_digest}
        )


if __name__ == "__main__":
    unittest.main()
