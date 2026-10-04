"""Build ledger step provenance tests: component identity and image ledger verification.

The ledger keys each image by a per-component identity rather than a whole-run
fingerprint:

1. **The component identity contains only what the step reads.** The whole-run
   fingerprint mixed the inputs of unrelated components -- the contracts
   commit, the contracts adapter and the contracts recipe cannot change a
   chain image, because only the Gonka image steps declare
   ``produces_images`` -- so a contracts-only change invalidated the whole
   network. ``Builder.component_identity`` narrows the identity to the inputs
   the step really reads (the selected ``gonka_sha``, ``gonka_version``, the
   recipe fingerprint, adapter content hash, platform and runner image), and
   ignores retired ``prepared_sha`` fields.

2. **Self-healing rebuild when an old image cannot be proven.** A comparison
   that raised before ``_remember_image`` could ever run meant no rerun could
   repair the ledger without a manual ``docker image rm``. The one-shot
   remove-and-rebuild retry turns "this image cannot be proven" into a fact
   the build itself establishes (``REBUILT_AFTER_REMOVAL``).

Every test below pins one side of that contract together with the concrete
negative case it must still reject. The only things faked are the process
boundaries: the docker CLI / build command under :class:`Builder`. The
decisions under test -- what an identity contains, whether an image is proven,
whether to retry -- are the production ones.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from forward_e2e.execution.builder import (
    Builder,
    IMAGE_LEDGER_SCHEMA,
    image_ledger_path,
    ledger_key,
    load_image_ledger,
    source_fingerprint,
    step_source_roles,
)
from forward_e2e.execution.compat import (
    StageCopy,
    gonka_immutable_source_adapter,
    marketplace_contracts_adapter,
)
from forward_e2e.execution.errors import BuildProvenanceError
from forward_e2e.execution.runlock import ManifestStatus
from tests.unit.runner.support.fakes import (
    BUILD_STARTED_EPOCH,
    CONTRACTS_SHA,
    FakeDockerDaemon,
    PREPARED_SHA,
    REQUESTED_SHA,
    completed,
    forbidden_runner,
    new_manifest,
    write_gonka_tree,
)

TEMP_PREFIX = "a8-test-e2e-r11bld-"

#: The first image the real chain recipe declares.
CHAIN_IMAGE = "ghcr.io/product-science/inferenced:latest"
#: The real step and recipe ids of the immutable-source chain build.
CHAIN_STEP_ID = "inferenced-image"
UPSTREAM_RECIPE_ID = "gonka-immutable-images-v2"
GONKA_VERSION = "v0.2.9-12-ge86e489"

#: The runner image the build ran in, as ``executor.py`` puts it into the
#: build context (``runner_image_id``).
RUNNER_IMAGE_ID = "sha256:" + "1" * 64
DOCKER_PLATFORM = "linux/amd64"

#: An image Docker created an hour before this build started: the case where
#: only the ledger can say whether it is a legitimate cache hit.
OLD_IMAGE_EPOCH = BUILD_STARTED_EPOCH - 3600.0

#: A Gonka commit that differs from :data:`REQUESTED_SHA` by nothing but its
#: value, used for single-fact negative variants.
OTHER_GONKA_SHA = "f" * 40
OTHER_CONTRACTS_SHA = "9" * 40


# ---------------------------------------------------------------------------
# 1. the component identity contains what the step reads, and only that
# ---------------------------------------------------------------------------
class ChainImageComponentIdentityTests(unittest.TestCase):
    """What the chain images are allowed to depend on.

    Every input below is taken from the real adapters, the real recipe and the
    real build context ``forward_e2e/execution/executor.py`` assembles; no fingerprint is
    written down in advance.
    """

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name).resolve()
        self.gonka = write_gonka_tree(self.root / "gonka")
        self.contracts = self.root / "contracts"
        self.contracts.mkdir(parents=True)
        self.build_out = self.root / "build-out"
        self.build_out.mkdir(parents=True)

        self.gonka_adapter = gonka_immutable_source_adapter()
        self.contracts_adapter = marketplace_contracts_adapter()
        self.chain_recipe = self.gonka_adapter.build
        self.chain_step = next(
            step for step in self.chain_recipe.steps if step.step_id == CHAIN_STEP_ID
        )
        self.contracts_recipe = self.contracts_adapter.build

        # The same context executor.py builds.
        self.context = {
            "gonka": str(self.gonka),
            "contracts": str(self.contracts),
            "build_out": str(self.build_out),
            "platform": DOCKER_PLATFORM,
            "goarch": "amd64",
            "gonka_sha": REQUESTED_SHA,
            "gonka_version": GONKA_VERSION,
            "contracts_sha": CONTRACTS_SHA,
            "runner_image_id": RUNNER_IMAGE_ID,
            "python": "/usr/local/bin/python3",
        }
        self.roles = {
            "gonka": self.gonka,
            "contracts": self.contracts,
            "build_out": self.build_out,
        }
        self.builder = Builder(runner=forbidden_runner)
        self.baseline = self.identity()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def identity(self, *, step=None, recipe=None, adapter=None, **context_overrides):
        context = dict(self.context, **context_overrides)
        return self.builder.component_identity(
            step if step is not None else self.chain_step,
            recipe=recipe if recipe is not None else self.chain_recipe,
            adapter=adapter if adapter is not None else self.gonka_adapter,
            context=context,
            roles=self.roles,
        )

    def run_level_fingerprint(self, *, contracts_sha=CONTRACTS_SHA):
        """The whole-run fingerprint executor.py computes.

        Reproduced here to show what the component identity deliberately does
        *not* inherit from it.
        """
        return source_fingerprint(
            {
                "gonka_sha": self.context["gonka_sha"],
                "contracts_sha": contracts_sha,
                "gonka_adapter": self.gonka_adapter.adapter_id,
                "contracts_adapter": self.contracts_adapter.adapter_id,
                "gonka_recipe": getattr(self.gonka_adapter.build, "recipe_id", None),
                "contracts_recipe": getattr(self.contracts_adapter.build, "recipe_id", None),
                "gonka_recipe_fingerprint": self.gonka_adapter.build.fingerprint(),
                "contracts_recipe_fingerprint": self.contracts_adapter.build.fingerprint(),
                "platform": DOCKER_PLATFORM,
            }
        )

    # -- which trees the step reads ------------------------------------
    def test_the_chain_image_step_reads_the_gonka_tree_and_no_other_source(self):
        self.assertEqual(self.chain_step.step_id, CHAIN_STEP_ID)
        self.assertEqual(self.chain_step.produces_images, (CHAIN_IMAGE,))

        self.assertEqual(step_source_roles(self.chain_step, self.roles), ["gonka"])
        # The negative case: the contracts tree is offered and is not taken,
        # because no argv token and no environment value names it.
        self.assertIn("contracts", self.roles)
        self.assertNotIn(
            "{contracts}",
            " ".join([*self.chain_step.argv, *dict(self.chain_step.env).values()]),
        )

    def test_the_contracts_release_step_reads_the_contracts_tree_and_no_other_source(self):
        a9_release = self.contracts_recipe.steps[0]

        self.assertEqual(a9_release.step_id, "a9-release")
        self.assertEqual(step_source_roles(a9_release, self.roles), ["contracts"])
        # Neither contracts step produces images; chain images are declared
        # by the Gonka recipe.
        self.assertEqual(a9_release.produces_images, ())
        self.assertEqual(
            [step.step_id for step in self.contracts_recipe.steps if step.produces_images],
            [],
        )

    # -- inputs that must NOT change the identity ----------------------
    def test_changing_only_the_contracts_commit_leaves_the_chain_image_identity_untouched(self):
        contracts_only = self.identity(contracts_sha=OTHER_CONTRACTS_SHA)

        self.assertEqual(contracts_only, self.baseline)
        # The negative case, and the defect this replaces: the whole-run
        # fingerprint does move, and keying images by it invalidated every
        # chain image on a contracts-only change.
        self.assertNotEqual(
            self.run_level_fingerprint(contracts_sha=OTHER_CONTRACTS_SHA),
            self.run_level_fingerprint(),
        )

    def test_the_gonka_input_is_the_selected_commit_and_gonka_version_because_that_is_what_is_built(self):
        selected_changed = self.identity(gonka_sha=OTHER_GONKA_SHA)
        version_changed = self.identity(gonka_version="v0.2.9-99-gffffffff")
        retired_prepared_injected = self.identity(prepared_sha=PREPARED_SHA)

        self.assertNotEqual(selected_changed, self.baseline)
        self.assertNotEqual(version_changed, self.baseline)
        # Retired prepared_sha keys are ignored in the immutable-source model.
        self.assertEqual(retired_prepared_injected, self.baseline)

    # -- inputs that MUST change the identity --------------------------
    def test_changing_a_sibling_staging_step_in_the_recipe_changes_the_chain_image_identity(self):
        stage_step = self.chain_recipe.steps[0]
        changed_stage = dataclasses.replace(
            stage_step,
            stage=stage_step.stage
            + (
                StageCopy(
                    source="{gonka}/README.md",
                    destination="{build_out}/extra/README.md",
                    what="extra staged file",
                ),
            ),
        )
        changed_recipe = dataclasses.replace(
            self.chain_recipe,
            steps=(changed_stage, *self.chain_recipe.steps[1:]),
        )
        variant_adapter = dataclasses.replace(self.gonka_adapter, build=changed_recipe)

        self.assertEqual(variant_adapter.adapter_id, self.gonka_adapter.adapter_id)
        self.assertNotEqual(variant_adapter.content_hash(), self.gonka_adapter.content_hash())
        self.assertNotEqual(
            self.identity(recipe=changed_recipe, adapter=variant_adapter), self.baseline
        )

    def test_changing_the_build_command_changes_the_identity_even_under_the_same_recipe_id(self):
        changed_step = dataclasses.replace(
            self.chain_step,
            argv=self.chain_step.argv + ("--build-arg", "BLST_PORTABLE=0"),
        )
        changed_recipe = dataclasses.replace(self.chain_recipe, steps=(changed_step,))

        self.assertEqual(changed_recipe.recipe_id, self.chain_recipe.recipe_id)
        self.assertNotEqual(
            self.identity(step=changed_step, recipe=changed_recipe), self.baseline
        )

    def test_changing_the_build_environment_of_the_step_changes_the_identity(self):
        changed_step = dataclasses.replace(
            self.chain_step,
            env=dict(self.chain_step.env, BLST_PORTABLE="0"),
        )

        self.assertNotEqual(self.identity(step=changed_step), self.baseline)

    def test_renaming_the_recipe_without_touching_the_command_changes_the_identity(self):
        renamed = dataclasses.replace(self.chain_recipe, recipe_id="gonka-immutable-images-v3")

        self.assertEqual(renamed.steps, self.chain_recipe.steps)
        self.assertNotEqual(self.identity(recipe=renamed), self.baseline)

    def test_changing_a_runtime_expectation_changes_the_chain_image_identity(self):
        # One mandatory fact: where the expected wasmd version is measured
        # from. Everything else about the adapter is untouched.
        changed_runtime = tuple(
            dataclasses.replace(expectation, go_mod_relpath="decentralized-api/go.mod")
            if expectation.field_name == "wasmd"
            else expectation
            for expectation in self.gonka_adapter.runtime
        )
        changed_adapter = dataclasses.replace(self.gonka_adapter, runtime=changed_runtime)

        self.assertNotEqual(changed_runtime, self.gonka_adapter.runtime)
        self.assertNotEqual(self.identity(adapter=changed_adapter), self.baseline)

    def test_changing_the_target_platform_changes_the_chain_image_identity(self):
        self.assertNotEqual(self.identity(platform="linux/arm64"), self.baseline)

    def test_building_in_another_runner_image_changes_the_chain_image_identity(self):
        self.assertNotEqual(
            self.identity(runner_image_id="sha256:" + "2" * 64), self.baseline
        )

    def test_changing_only_the_adapter_id_changes_the_chain_image_identity(self):
        other_adapter = dataclasses.replace(
            self.gonka_adapter,
            adapter_id="gonka-other-v1",
        )
        # Keep the recipe unchanged so its identity cannot mask an ignored
        # adapter id. Recipe-id changes have their own test above.
        self.assertNotEqual(
            self.identity(adapter=other_adapter),
            self.baseline,
        )


# ---------------------------------------------------------------------------
# shared fixture for the ledger behaviour of a real chain build
# ---------------------------------------------------------------------------
class ChainBuildTestCase(unittest.TestCase):
    """One real chain step, one declared image, a real ledger on disk."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
        self.root = Path(self.tmp_dir.name).resolve()
        self.gonka = write_gonka_tree(self.root / "gonka")
        self.contracts = self.root / "contracts"
        self.contracts.mkdir(parents=True)
        self.build_out = self.root / "build-out"
        self.build_out.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.log_dir = self.root / "logs"
        self.ledger = image_ledger_path(self.workspace)
        self.emitted = []

        self.adapter = gonka_immutable_source_adapter()
        self.recipe = self.narrowed_recipe(self.adapter.build)
        self.step = self.recipe.steps[0]
        self.context = {
            "gonka": str(self.gonka),
            "contracts": str(self.contracts),
            "build_out": str(self.build_out),
            "platform": DOCKER_PLATFORM,
            "goarch": "amd64",
            "gonka_sha": REQUESTED_SHA,
            "gonka_version": GONKA_VERSION,
            "contracts_sha": CONTRACTS_SHA,
            "runner_image_id": RUNNER_IMAGE_ID,
            "python": "/usr/local/bin/python3",
        }
        self.roles = {
            "gonka": self.gonka,
            "contracts": self.contracts,
            "build_out": self.build_out,
        }
        #: The whole-run fingerprint executor.py hands to the Builder. It is
        #: context in the ledger, never a cache decision.
        self.run_fingerprint = source_fingerprint(
            {
                "gonka_sha": REQUESTED_SHA,
                "contracts_sha": CONTRACTS_SHA,
                "gonka_adapter": self.adapter.adapter_id,
                "contracts_adapter": marketplace_contracts_adapter().adapter_id,
                "gonka_recipe": self.adapter.build.recipe_id,
                "contracts_recipe": marketplace_contracts_adapter().build.recipe_id,
                "gonka_recipe_fingerprint": self.adapter.build.fingerprint(),
                "contracts_recipe_fingerprint": marketplace_contracts_adapter().build.fingerprint(),
                "platform": DOCKER_PLATFORM,
            }
        )
        self.identity = self.component_identity()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    @staticmethod
    def narrowed_recipe(recipe, *, images=(CHAIN_IMAGE,)):
        """The real chain step, narrowed to the images under test."""
        base_step = next(s for s in recipe.steps if s.step_id == CHAIN_STEP_ID)
        step = dataclasses.replace(base_step, produces_images=tuple(images))
        return dataclasses.replace(recipe, steps=(step,))

    def component_identity(self, *, adapter=None, **context_overrides):
        """The identity the production code computes for this exact step."""
        return Builder(runner=forbidden_runner).component_identity(
            self.step,
            recipe=self.recipe,
            adapter=adapter if adapter is not None else self.adapter,
            context=dict(self.context, **context_overrides),
            roles=self.roles,
        )

    def make_builder(self, daemon, *, with_logs=False):
        return Builder(
            runner=daemon,
            log_dir=self.log_dir if with_logs else None,
            emit=self.emitted.append,
            ledger_path=self.ledger,
            source_fingerprint=self.run_fingerprint,
        )

    def build(self, builder, *, manifest=None, started_epoch=BUILD_STARTED_EPOCH):
        manifest = manifest if manifest is not None else new_manifest()
        builder.run_recipe(
            self.recipe,
            manifest=manifest,
            context=self.context,
            roles=self.roles,
            started_epoch=started_epoch,
            adapter=self.adapter,
        )
        return manifest

    def write_ledger(self, *, identities, reference=CHAIN_IMAGE, image_id=None):
        """A ledger entry in the shape ``_remember_image`` writes."""
        resolved_id = image_id or FakeDockerDaemon.image_id_for(reference)
        document = {
            "schema": IMAGE_LEDGER_SCHEMA,
            "images": {
                ledger_key(reference, resolved_id): {
                    "reference": reference,
                    "image_id": resolved_id,
                    "source_fingerprints": sorted(identities),
                    "run_fingerprints": [self.run_fingerprint],
                    "created_epoch": OLD_IMAGE_EPOCH,
                    "first_recorded_at_utc": "2026-01-01T00:00:00+00:00",
                    "last_recorded_at_utc": "2026-01-02T00:00:00+00:00",
                    "produced_by": [f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}"],
                }
            },
        }
        self.ledger.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return self.ledger

    def ledger_entry(self, reference=CHAIN_IMAGE):
        key = ledger_key(reference, FakeDockerDaemon.image_id_for(reference))
        return load_image_ledger(self.ledger).get(key)


# ---------------------------------------------------------------------------
# 2. a cache hit needs a matching identity AND a provable image
# ---------------------------------------------------------------------------
class ProvenCacheHitTests(ChainBuildTestCase):
    def test_an_old_image_recorded_against_this_components_identity_is_accepted_untouched(self):
        self.write_ledger(identities=[self.identity])
        daemon = FakeDockerDaemon.with_old_image()

        manifest = self.build(self.make_builder(daemon))

        self.assertEqual(manifest.failures, [])
        self.assertNotEqual(manifest.status, ManifestStatus.FAILED)
        self.assertEqual(len(manifest.images), 1)
        self.assertIs(manifest.images[0]["cache_hit"], True)
        self.assertEqual(manifest.images[0]["component_identity"], self.identity)
        self.assertIn(self.identity, manifest.images[0]["cache_proof"]["source_fingerprints"])
        # Nothing was removed and the step ran exactly once.
        self.assertEqual(daemon.removals, [])
        self.assertEqual(len(daemon.build_invocations), 1)

    def test_the_ledger_records_the_component_identity_as_proof_and_the_run_fingerprint_as_context(self):
        daemon = FakeDockerDaemon(
            images={
                CHAIN_IMAGE: {
                    "id": FakeDockerDaemon.image_id_for(CHAIN_IMAGE),
                    "created_epoch": BUILD_STARTED_EPOCH + 60.0,
                }
            }
        )

        self.build(self.make_builder(daemon))

        entry = self.ledger_entry()
        self.assertEqual(entry["source_fingerprints"], [self.identity])
        # The negative case: the whole-run fingerprint is kept only as context.
        # Recording it as an identity is what made a contracts-only change
        # invalidate this image.
        self.assertNotIn(self.run_fingerprint, entry["source_fingerprints"])
        self.assertEqual(entry["run_fingerprints"], [self.run_fingerprint])
        self.assertEqual(entry["produced_by"], [f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}"])

    def test_an_entry_recorded_before_the_gonka_commit_changed_is_not_accepted_as_proof(self):
        stale_identity = self.component_identity(gonka_sha=OTHER_GONKA_SHA)
        self.assertNotEqual(stale_identity, self.identity)
        self.write_ledger(identities=[stale_identity])
        # The image cannot be removed, so the self-healing retry cannot rescue
        # it either and the run must fail.
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "recorded against different sources")
        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "STALE_IMAGE_REUSED")
        self.assertEqual(failure["details"]["expected_component_identity"], self.identity)
        self.assertEqual(
            failure["details"]["recorded_source_fingerprints"], [stale_identity]
        )
        self.assertEqual(manifest.images, [])
        self.assertEqual(manifest.status, ManifestStatus.FAILED)

    def test_the_whole_run_fingerprint_on_its_own_does_not_authorise_the_image(self):
        # Exactly the record the previous implementation wrote and compared.
        self.write_ledger(identities=[self.run_fingerprint])
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertEqual(cm.exception.details["reason"], "recorded against different sources")
        self.assertEqual(
            manifest.failures[-1]["details"]["recorded_source_fingerprints"],
            [self.run_fingerprint],
        )

    def test_a_matching_identity_recorded_for_another_image_id_is_not_proof_of_this_one(self):
        self.write_ledger(identities=[self.identity], image_id="sha256:" + "0" * 64)
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertEqual(cm.exception.details["reason"], "no record of this image id")
        self.assertEqual(
            manifest.failures[-1]["details"]["image_id"],
            FakeDockerDaemon.image_id_for(CHAIN_IMAGE),
        )

    def test_a_missing_ledger_leaves_an_old_image_unprovable(self):
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertEqual(cm.exception.details["reason"], "no record of this image id")
        self.assertEqual(manifest.failures[-1]["details"]["rebuild_attempted"], False)


# ---------------------------------------------------------------------------
# 4. the one-shot self-healing retry
# ---------------------------------------------------------------------------
class SelfHealingRebuildTests(ChainBuildTestCase):
    """An unprovable image is removed and rebuilt once, or the run fails."""

    def test_a_failed_or_malformed_absence_probe_cannot_authorise_an_unchanged_image(self):
        for response in (
            {"returncode": 1, "stderr": "Cannot connect to the Docker daemon"},
            {"stdout": "not JSON"},
            {"stdout": "[]"},
        ):
            with self.subTest(response=response):
                # Start from the existing unremovable-image fixture and change
                # only the response to its post-removal inspection.
                daemon = FakeDockerDaemon.with_old_image(honour_removal=False)

                def runner(argv, **kwargs):
                    if argv[:3] == ["docker", "image", "inspect"] and daemon.removals:
                        return completed(argv, **response)
                    return daemon(argv, **kwargs)

                manifest = new_manifest()
                with self.assertRaises(BuildProvenanceError):
                    self.build(self.make_builder(runner), manifest=manifest)

                self.assertEqual(daemon.removals, [CHAIN_IMAGE])
                self.assertEqual(len(daemon.build_invocations), 1)
                self.assertEqual(manifest.images, [])
                self.assertFalse(self.ledger.exists())

    def test_an_unprovable_image_is_removed_rebuilt_and_recorded_as_rebuilt_after_removal(self):
        # A layer-cached rebuild legitimately returns the same image id with
        # its original creation time, so the timestamp still proves nothing;
        # the removal and the recreation together are the proof.
        daemon = FakeDockerDaemon.with_old_image()

        manifest = self.build(self.make_builder(daemon))

        self.assertEqual(manifest.failures, [])
        self.assertEqual(daemon.removals, [CHAIN_IMAGE])
        self.assertEqual(len(daemon.build_invocations), 2)
        image = manifest.images[0]
        self.assertIs(image["cache_hit"], True)
        self.assertEqual(image["cache_proof"]["kind"], "REBUILT_AFTER_REMOVAL")
        self.assertEqual(image["cache_proof"]["component_identity"], self.identity)
        self.assertEqual(image["cache_proof"]["image_id"], image["image_id"])
        self.assertTrue(
            any("rebuilt after removal" in line for line in self.emitted), self.emitted
        )

    def test_the_rebuilt_image_is_written_to_the_ledger_so_the_next_run_needs_no_removal(self):
        first_daemon = FakeDockerDaemon.with_old_image()
        self.build(self.make_builder(first_daemon))
        self.assertEqual(self.ledger_entry()["source_fingerprints"], [self.identity])

        # A second run of the same lock, with the same old image still there.
        second_daemon = FakeDockerDaemon.with_old_image()
        manifest = self.build(self.make_builder(second_daemon))

        # The negative case, and the deadlock this replaces: before the fix the
        # comparison raised before the recording could ever happen, so this
        # second run failed again and the only escape was `docker image rm`.
        self.assertEqual(manifest.failures, [])
        self.assertEqual(second_daemon.removals, [])
        self.assertEqual(len(second_daemon.build_invocations), 1)
        self.assertIs(manifest.images[0]["cache_hit"], True)
        self.assertNotIn("kind", manifest.images[0]["cache_proof"])
        self.assertIn(
            self.identity, manifest.images[0]["cache_proof"]["source_fingerprints"]
        )

    def test_the_removal_and_rebuild_are_attempted_at_most_once(self):
        daemon = FakeDockerDaemon.with_old_image()
        manifest = new_manifest()

        self.build(self.make_builder(daemon), manifest=manifest)

        # Even though the rebuilt image is *still* an undatable cache hit, the
        # loop stops after the second attempt instead of spinning.
        self.assertEqual(len(daemon.build_invocations), 2)
        self.assertEqual(daemon.removals, [CHAIN_IMAGE])
        self.assertEqual(
            [record["attempt"] for record in manifest.build_commands], [1, 2]
        )

    def test_an_image_that_survives_removal_fails_the_run_instead_of_being_accepted(self):
        # The daemon reports a successful removal, but the image is still
        # there: another tag holds it. Rebuilding would prove nothing.
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(daemon.removals, [CHAIN_IMAGE])
        # No second attempt: the step ran exactly once.
        self.assertEqual(len(daemon.build_invocations), 1)
        self.assertEqual([record["attempt"] for record in manifest.build_commands], [1])
        self.assertEqual(manifest.failures[-1]["code"], "STALE_IMAGE_REUSED")
        self.assertIs(manifest.failures[-1]["details"]["rebuild_attempted"], False)
        self.assertEqual(manifest.images, [])
        self.assertTrue(
            any("still present after removal" in line for line in self.emitted),
            self.emitted,
        )

    def test_an_image_that_the_retry_fails_to_recreate_stops_the_run(self):
        # The removal works, the second build produces nothing at all.
        daemon = FakeDockerDaemon.with_old_image(rebuild_restores=False)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(self.make_builder(daemon), manifest=manifest)

        self.assertIn("A declared image is missing after its build step", str(cm.exception))
        self.assertEqual(len(daemon.build_invocations), 2)
        self.assertEqual(manifest.failures[-1]["code"], "BUILD_IMAGE_MISSING")
        self.assertEqual(manifest.images, [])

    def test_a_freshly_created_image_is_never_removed_and_never_retried(self):
        daemon = FakeDockerDaemon(
            images={
                CHAIN_IMAGE: {
                    "id": FakeDockerDaemon.image_id_for(CHAIN_IMAGE),
                    "created_epoch": BUILD_STARTED_EPOCH + 60.0,
                }
            }
        )

        manifest = self.build(self.make_builder(daemon))

        self.assertEqual(manifest.failures, [])
        self.assertEqual(daemon.removals, [])
        self.assertEqual(len(daemon.build_invocations), 1)
        self.assertIs(manifest.images[0]["cache_hit"], False)
        self.assertNotIn("cache_proof", manifest.images[0])


# ---------------------------------------------------------------------------
# 5. the first attempt's evidence survives the retry
# ---------------------------------------------------------------------------
class RetryLogPreservationTests(ChainBuildTestCase):
    def build_log(self, suffix=""):
        return self.log_dir / f"build-{CHAIN_STEP_ID}{suffix}.log"

    def test_the_first_attempts_build_log_is_kept_beside_the_retrys_log(self):
        daemon = FakeDockerDaemon.with_old_image()

        self.build(self.make_builder(daemon, with_logs=True))

        self.assertEqual(len(daemon.build_invocations), 2)
        # The negative case: the retry must not write over the log that
        # explains why the first attempt could not be proven.
        self.assertEqual(self.build_log().read_text(encoding="utf-8"), "build attempt 1\n")
        self.assertEqual(
            self.build_log(".attempt-2").read_text(encoding="utf-8"), "build attempt 2\n"
        )

    def test_the_retry_adds_a_file_instead_of_renaming_the_historical_one(self):
        daemon = FakeDockerDaemon.with_old_image()

        self.build(self.make_builder(daemon, with_logs=True))

        # Existing tooling and documentation look for the unsuffixed name, so
        # the retry is written next to it rather than in place of it.
        self.assertEqual(
            sorted(path.name for path in self.log_dir.iterdir()),
            sorted(
                [
                    f"build-{CHAIN_STEP_ID}.log",
                    f"build-{CHAIN_STEP_ID}.attempt-2.log",
                ]
            ),
        )

    def test_a_build_without_a_retry_writes_the_one_historical_log_and_no_attempt_file(self):
        self.write_ledger(identities=[self.identity])
        daemon = FakeDockerDaemon.with_old_image()

        self.build(self.make_builder(daemon, with_logs=True))

        self.assertEqual(len(daemon.build_invocations), 1)
        self.assertEqual(
            [path.name for path in self.log_dir.iterdir()], [f"build-{CHAIN_STEP_ID}.log"]
        )

    def test_the_failed_first_attempt_of_an_unremovable_image_still_leaves_its_log(self):
        daemon = FakeDockerDaemon.with_old_image(honour_removal=False)

        with self.assertRaises(BuildProvenanceError):
            self.build(self.make_builder(daemon, with_logs=True))

        self.assertEqual(self.build_log().read_text(encoding="utf-8"), "build attempt 1\n")
        self.assertFalse(self.build_log(".attempt-2").exists())


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
