"""Regression tests for the image provenance ledger in ops.a8.e2e.builder.

Docker is content addressable: rebuilding unchanged sources legitimately
returns the *previous* image, which keeps its original creation time. The
timestamp-only staleness check therefore rejected correct builds. The ledger
records which source identities produced which image id, so a cached image is
accepted only when it is proven to come from the very sources under test,
while unknown, undated or differently-sourced images stay refused.

These tests pin every side of that contract: the legitimate cache hit is
accepted, an image whose creation time cannot be read now needs the same proof
as an old one, several identities can legitimately share an image id and none
of them may be forgotten, and a damaged ledger is preserved rather than
silently overwritten.

What round 11 changed underneath these claims
---------------------------------------------
Finding F2 replaced the *run-level* source fingerprint with a per-component
identity as the thing a cache decision is made on. ``Builder.component_identity``
derives it from the step's own content, the recipe id, the adapter's content
hash, the platform, the runner image and only the source roles that step
actually reads. The run-level fingerprint survives in the ledger under
``run_fingerprints`` as context for a human, and is never compared.

Every seeded ledger entry below therefore carries an identity obtained from
the production code itself (``LedgerTestCase.identity_for``) rather than a
literal: a hardcoded string would only prove that the ledger stores what it is
given. Each refusal these tests made in round 10 is still made here, now
against the component identity.

The deeper behaviour F2 added -- the self-healing rebuild, the retry log and
what the identity is composed of -- is covered in
``ops/a8/tests/test_e2e_build_ledger.py`` and is deliberately not
duplicated here.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.a8.e2e.builder import (
    Builder,
    IMAGE_LEDGER_FILENAME,
    IMAGE_LEDGER_SCHEMA,
    image_ledger_path,
    ledger_fingerprints,
    ledger_is_damaged,
    ledger_key,
    load_image_ledger,
    source_fingerprint,
)
from ops.a8.e2e.compat import (
    gonka_immutable_source_adapter,
)
from ops.a8.e2e.errors import BuildProvenanceError
from ops.a8.e2e.runlock import ManifestStatus
from ops.a8.tests.support.fakes import (
    BUILD_STARTED_EPOCH,
    CONTRACTS_SHA,
    FakeBuildRunner,
    FakeDockerDaemon,
    REQUESTED_SHA,
    completed,
    new_manifest,
    write_gonka_tree,
)

#: The first image the real chain recipe declares.
CHAIN_IMAGE = "ghcr.io/product-science/inferenced:latest"

#: The real recipe ids of the two chain builds, as recorded in the ledger.
UPSTREAM_RECIPE_ID = "gonka-immutable-images-v2"
LEGACY_RECIPE_ID = "gonka-legacy-images-v1"
CHAIN_STEP_ID = "inferenced-image"

#: Docker reports this image as created one minute into the first build.
FRESH_IMAGE_EPOCH = BUILD_STARTED_EPOCH + 60.0
#: ... which makes it an *old* image for a build started an hour later.
LATER_BUILD_EPOCH = BUILD_STARTED_EPOCH + 3600.0
OLD_IMAGE_EPOCH = BUILD_STARTED_EPOCH - 3600.0

SELECTED_SOURCES = {
    "gonka_sha": REQUESTED_SHA,
    "contracts_sha": CONTRACTS_SHA,
    "adapter_id": "gonka-immutable-source-v2",
    "platform": "linux/amd64",
}
OTHER_SOURCES = dict(SELECTED_SOURCES, gonka_sha="9" * 40)


class UnreadableCreationTimeRunner(FakeBuildRunner):
    """Docker answers with a creation time this tool cannot parse.

    A corrupt, empty or unexpectedly formatted ``Created`` field used to make
    the image bypass the staleness check entirely; it must now be treated like
    any other image the build cannot prove it produced.
    """

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = [str(a) for a in argv]
        if argv[:3] == ["docker", "image", "inspect"]:
            reference = argv[3]
            if reference in self.missing_images:
                return completed(argv, returncode=1, stderr="No such image")
            payload = [
                {
                    "Id": self.image_id_for(reference),
                    "RepoDigests": [],
                    "Created": "a while ago",
                }
            ]
            return completed(argv, stdout=json.dumps(payload))
        return super().__call__(argv, cwd=cwd, env=env, timeout=timeout)


class LedgerTestCase(unittest.TestCase):
    """Shared synthetic workspace and real chain build recipes."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-ledger-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.gonka = write_gonka_tree(self.root / "gonka")
        self.build_out = self.root / "build-out"
        self.build_out.mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.ledger = image_ledger_path(self.workspace)
        self.fingerprint = source_fingerprint(SELECTED_SOURCES)
        self.other_fingerprint = source_fingerprint(OTHER_SOURCES)
        self.emitted = []
        self.adapter = gonka_immutable_source_adapter()
        self.context = {
            "gonka": str(self.gonka),
            "build_out": str(self.build_out),
            "platform": "linux/amd64",
            "goarch": "amd64",
            "gonka_sha": REQUESTED_SHA,
            "gonka_version": "v0.2.9-12-ge86e489",
        }
        #: The same build, one input different: the commit that is built.
        #: ``OTHER_SOURCES`` changes the same field, so the run-level and the
        #: component-level "other" identities disagree for the same reason.
        self.other_context = dict(self.context, gonka_sha="9" * 40)
        self.roles = {"gonka": self.gonka, "build_out": self.build_out}
        #: What round 11 actually compares. Derived, never hardcoded -- see
        #: ``identity_for``.
        self.identity = self.identity_for()
        self.other_identity = self.identity_for(context=self.other_context)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # -- helpers -------------------------------------------------------
    def chain_recipe(self, *, images=(CHAIN_IMAGE,)):
        """The real upstream recipe, narrowed to the images under test."""
        recipe = self.adapter.build
        base_step = next(s for s in recipe.steps if s.step_id == CHAIN_STEP_ID)
        step = dataclasses.replace(base_step, produces_images=tuple(images))
        return dataclasses.replace(recipe, steps=(step,))

    def legacy_chain_recipe(self, *, images=(CHAIN_IMAGE,)):
        """A distinct chain recipe: same images, different recipe id."""
        recipe = dataclasses.replace(self.adapter.build, recipe_id=LEGACY_RECIPE_ID)
        base_step = next(s for s in recipe.steps if s.step_id == CHAIN_STEP_ID)
        step = dataclasses.replace(base_step, produces_images=tuple(images))
        return dataclasses.replace(recipe, steps=(step,))

    def identity_for(self, *, recipe=None, context=None, adapter=...):
        """The component identity the production code will derive for a step.

        Round 11 (finding F2) moved the cache decision from the run-level
        source fingerprint to a per-component identity computed by
        ``Builder.component_identity`` from the step content, the recipe id,
        the *adapter's* content hash, the platform, the runner image and only
        the source roles the step really reads.

        Seeding the ledger with a hardcoded string would prove nothing -- it
        would only prove that the ledger stores whatever it is given. So the
        identity is asked of the production code itself, from the same real
        adapter and recipe the build under test will use.

        The identity covers ``content_sha256(step.to_dict())``, so a recipe
        narrowed to a different set of ``produces_images`` has a *different*
        identity: any caller that narrows the recipe must seed with
        ``identity_for(recipe=that_recipe)``.
        """
        recipe = recipe if recipe is not None else self.chain_recipe()
        probe = Builder(
            runner=FakeBuildRunner(),
            emit=self.emitted.append,
            ledger_path=None,
            source_fingerprint=None,
        )
        return probe.component_identity(
            recipe.steps[0],
            recipe=recipe,
            adapter=self.adapter if adapter is ... else adapter,
            context=context if context is not None else self.context,
            roles=self.roles,
        )

    def make_builder(self, runner, *, ledger_path=..., fingerprint=...):
        return Builder(
            runner=runner,
            emit=self.emitted.append,
            ledger_path=self.ledger if ledger_path is ... else ledger_path,
            source_fingerprint=(
                self.fingerprint if fingerprint is ... else fingerprint
            ),
        )

    def build(
        self,
        builder,
        *,
        started_epoch=BUILD_STARTED_EPOCH,
        recipe=None,
        manifest=None,
        context=None,
        adapter=...,
    ):
        """Run a recipe the way ``ops.a8.e2e.executor`` runs it.

        The ``adapter=`` keyword is passed because the executor passes it: it
        is part of the component identity, so omitting it here would silently
        test a different identity than production computes.
        """
        manifest = manifest if manifest is not None else new_manifest()
        builder.run_recipe(
            recipe if recipe is not None else self.chain_recipe(),
            manifest=manifest,
            context=context if context is not None else self.context,
            roles=self.roles,
            started_epoch=started_epoch,
            adapter=self.adapter if adapter is ... else adapter,
        )
        return manifest

    def image_id(self, reference=CHAIN_IMAGE):
        return FakeBuildRunner().image_id_for(reference)

    def write_ledger(self, entries):
        document = {"schema": IMAGE_LEDGER_SCHEMA, "images": dict(entries)}
        self.ledger.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return self.ledger

    def record_for(
        self,
        *,
        reference=CHAIN_IMAGE,
        fingerprints=None,
        created_epoch=OLD_IMAGE_EPOCH,
        image_id=None,
        recipe=None,
    ):
        """A ledger entry in the current (list of identities) shape.

        The default proof is the component identity of ``recipe`` (the chain
        recipe when none is given), because that is the only value round 11
        accepts as proof.
        """
        resolved_id = image_id if image_id is not None else self.image_id(reference)
        return {
            ledger_key(reference, resolved_id): {
                "reference": reference,
                "image_id": resolved_id,
                "source_fingerprints": sorted(
                    fingerprints
                    if fingerprints is not None
                    else [self.identity_for(recipe=recipe)]
                ),
                "run_fingerprints": [self.fingerprint],
                "created_epoch": created_epoch,
                "first_recorded_at_utc": "2024-01-01T00:00:00+00:00",
                "last_recorded_at_utc": "2024-01-02T00:00:00+00:00",
                "produced_by": [f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}"],
            }
        }

    def legacy_record_for(self, *, reference=CHAIN_IMAGE, fingerprint=None):
        """A ledger entry as written before identities became a list."""
        resolved_id = self.image_id(reference)
        return {
            ledger_key(reference, resolved_id): {
                "reference": reference,
                "image_id": resolved_id,
                "source_fingerprint": (
                    fingerprint if fingerprint is not None else self.identity
                ),
                "step_id": CHAIN_STEP_ID,
                "recipe_id": UPSTREAM_RECIPE_ID,
                "created_epoch": OLD_IMAGE_EPOCH,
                "recorded_at_utc": "2024-01-01T00:00:00+00:00",
            }
        }

    def ledger_entry(self, reference=CHAIN_IMAGE):
        return load_image_ledger(self.ledger)[ledger_key(reference, self.image_id(reference))]

    def corrupt_backups(self):
        return sorted(
            p for p in self.workspace.iterdir()
            if p.name.startswith(IMAGE_LEDGER_FILENAME + ".corrupt-")
        )


class SourceFingerprintTests(unittest.TestCase):
    """The identity that decides whether a cached image may be reused."""

    def test_fingerprint_of_the_same_selection_is_identical_whatever_the_key_order(self):
        forward = source_fingerprint(
            {"gonka_sha": REQUESTED_SHA, "contracts_sha": CONTRACTS_SHA, "platform": "linux/amd64"}
        )
        backward = source_fingerprint(
            {"platform": "linux/amd64", "contracts_sha": CONTRACTS_SHA, "gonka_sha": REQUESTED_SHA}
        )

        self.assertEqual(forward, backward)

    def test_fingerprint_changes_when_any_single_selected_source_changes(self):
        baseline = source_fingerprint(SELECTED_SOURCES)

        for key in sorted(SELECTED_SOURCES):
            changed = dict(SELECTED_SOURCES)
            changed[key] = str(changed[key]) + "-changed"
            with self.subTest(changed=key):
                self.assertNotEqual(source_fingerprint(changed), baseline)

    def test_fingerprint_changes_when_a_source_is_added_or_removed(self):
        baseline = source_fingerprint(SELECTED_SOURCES)
        extended = dict(SELECTED_SOURCES, extra_repo_sha="0" * 40)
        reduced = {k: v for k, v in SELECTED_SOURCES.items() if k != "platform"}

        self.assertNotEqual(source_fingerprint(extended), baseline)
        self.assertNotEqual(source_fingerprint(reduced), baseline)

    def test_fingerprint_is_a_hexadecimal_sha256_digest(self):
        digest = source_fingerprint(SELECTED_SOURCES)

        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        int(digest, 16)  # raises ValueError if it is not hexadecimal


class LedgerFingerprintReadingTests(unittest.TestCase):
    """One image id may belong to several identities; none may be lost."""

    def test_a_missing_entry_records_no_identity_at_all(self):
        self.assertEqual(ledger_fingerprints(None), [])

    def test_an_entry_that_is_not_a_mapping_records_no_identity(self):
        self.assertEqual(ledger_fingerprints("sha256:abc"), [])
        self.assertEqual(ledger_fingerprints(["sha256:abc"]), [])

    def test_an_entry_without_any_identity_field_records_no_identity(self):
        self.assertEqual(ledger_fingerprints({"image_id": "sha256:abc"}), [])

    def test_every_identity_in_the_recorded_list_is_reported(self):
        entry = {"source_fingerprints": ["aaa", "bbb"]}

        self.assertEqual(ledger_fingerprints(entry), ["aaa", "bbb"])

    def test_a_ledger_written_before_the_list_existed_is_still_understood(self):
        entry = {"source_fingerprint": "aaa"}

        self.assertEqual(ledger_fingerprints(entry), ["aaa"])

    def test_the_singular_key_is_merged_with_the_list_without_duplicating_it(self):
        merged = ledger_fingerprints(
            {"source_fingerprints": ["aaa", "bbb"], "source_fingerprint": "aaa"}
        )
        added = ledger_fingerprints(
            {"source_fingerprints": ["aaa"], "source_fingerprint": "ccc"}
        )

        self.assertEqual(merged, ["aaa", "bbb"])
        self.assertEqual(added, ["aaa", "ccc"])

    def test_empty_identities_are_never_reported_as_proof(self):
        self.assertEqual(
            ledger_fingerprints({"source_fingerprints": ["", None], "source_fingerprint": ""}),
            [],
        )


class LedgerDocumentTests(LedgerTestCase):
    """Reading the ledger must never invent provenance that is not there."""

    def test_ledger_lives_in_a_single_named_file_inside_the_persistent_workspace(self):
        self.assertEqual(image_ledger_path(self.workspace).name, IMAGE_LEDGER_FILENAME)
        self.assertEqual(image_ledger_path(self.workspace).parent, self.workspace)
        self.assertEqual(image_ledger_path(str(self.workspace)), self.ledger)

    def test_ledger_key_distinguishes_both_the_reference_and_the_image_id(self):
        key = ledger_key(CHAIN_IMAGE, "sha256:abc")

        self.assertIn(CHAIN_IMAGE, key)
        self.assertIn("sha256:abc", key)
        self.assertNotEqual(key, ledger_key(CHAIN_IMAGE, "sha256:def"))
        self.assertNotEqual(key, ledger_key("other:latest", "sha256:abc"))

    def test_absent_ledger_file_reads_as_empty_so_no_old_image_can_be_proven(self):
        self.assertFalse(self.ledger.exists())

        self.assertEqual(load_image_ledger(self.ledger), {})

    def test_ledger_path_of_none_reads_as_empty(self):
        self.assertEqual(load_image_ledger(None), {})

    def test_corrupt_ledger_reads_as_empty_so_corruption_fails_closed(self):
        self.ledger.write_text("{not json at all", encoding="utf-8")

        self.assertEqual(load_image_ledger(self.ledger), {})

    def test_ledger_whose_images_section_is_not_a_mapping_reads_as_empty(self):
        self.ledger.write_text(
            json.dumps({"schema": IMAGE_LEDGER_SCHEMA, "images": ["sha256:abc"]}),
            encoding="utf-8",
        )

        self.assertEqual(load_image_ledger(self.ledger), {})

    def test_ledger_that_is_a_json_list_rather_than_a_document_reads_as_empty(self):
        self.ledger.write_text(json.dumps(["sha256:abc"]), encoding="utf-8")

        self.assertEqual(load_image_ledger(self.ledger), {})

    def test_well_formed_ledger_yields_its_recorded_entries_keyed_by_reference_and_id(self):
        entries = self.record_for()
        self.write_ledger(entries)

        loaded = load_image_ledger(self.ledger)

        self.assertEqual(sorted(loaded), sorted(entries))
        self.assertEqual(
            ledger_fingerprints(loaded[ledger_key(CHAIN_IMAGE, self.image_id())]),
            [self.identity],
        )


class FreshImageRecordingTests(LedgerTestCase):
    """An image built during the run needs no proof, but is remembered."""

    def test_image_created_after_the_build_started_is_accepted_and_not_a_cache_hit(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertEqual(len(manifest.images), 1)
        entry = manifest.images[0]
        self.assertIs(entry["cache_hit"], False)
        self.assertNotIn("cache_proof", entry)
        self.assertEqual(entry["image_id"], self.image_id())
        self.assertEqual(entry["role"], CHAIN_IMAGE)
        self.assertEqual(entry["step_id"], CHAIN_STEP_ID)

    def test_image_built_now_is_written_into_the_ledger_against_this_builders_sources(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)

        self.build(builder)

        document = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], IMAGE_LEDGER_SCHEMA)
        recorded = document["images"][ledger_key(CHAIN_IMAGE, self.image_id())]
        # The proof is the component identity: what this *step* consumed.
        self.assertEqual(recorded["source_fingerprints"], [self.identity])
        # The run-level fingerprint is kept beside it as context for a human
        # reading the ledger. It is never compared, and it is not the proof.
        self.assertEqual(recorded["run_fingerprints"], [self.fingerprint])
        self.assertNotEqual(self.identity, self.fingerprint)
        self.assertEqual(recorded["reference"], CHAIN_IMAGE)
        self.assertEqual(recorded["image_id"], self.image_id())
        self.assertAlmostEqual(recorded["created_epoch"], FRESH_IMAGE_EPOCH, places=3)
        self.assertTrue(recorded["first_recorded_at_utc"])
        self.assertTrue(recorded["last_recorded_at_utc"])
        # The superseded singular keys must not come back.
        self.assertNotIn("source_fingerprint", recorded)
        self.assertNotIn("recorded_at_utc", recorded)

    def test_the_record_names_the_recipe_and_the_step_that_produced_the_image(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)

        self.build(builder)

        self.assertEqual(
            self.ledger_entry()["produced_by"],
            [f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}"],
        )
        self.assertEqual(self.adapter.build.recipe_id, UPSTREAM_RECIPE_ID)

    def test_every_image_the_step_declares_is_remembered_under_its_own_key(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)
        declared = self.adapter.build.chain_images
        multi_recipe = self.chain_recipe(images=declared)

        manifest = self.build(builder, recipe=multi_recipe)

        self.assertEqual(len(manifest.images), len(declared))
        images = load_image_ledger(self.ledger)
        self.assertEqual(
            sorted(images),
            sorted(ledger_key(ref, self.image_id(ref)) for ref in declared),
        )

    def test_a_builder_without_a_run_fingerprint_still_attributes_what_it_built(self):
        """Round 11 made attribution unconditional.

        Round 10 could not attribute an image when the ``Builder`` was given
        no ``source_fingerprint``, so it recorded nothing at all. The component
        identity is derived from the step, the recipe, the adapter and the
        context, none of which depend on that argument, so there is no longer
        a build that cannot say what it produced. What disappears without a
        run-level fingerprint is only the *context* line beside the proof.
        """
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner, fingerprint=None)

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertIs(manifest.images[0]["cache_hit"], False)
        self.assertTrue(self.ledger.exists())
        self.assertEqual(self.ledger_entry()["source_fingerprints"], [self.identity])
        self.assertEqual(self.ledger_entry()["run_fingerprints"], [])

    def test_builder_without_a_ledger_path_builds_and_writes_no_ledger_anywhere(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner, ledger_path=None)

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertIs(manifest.images[0]["cache_hit"], False)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_the_ledger_is_left_as_one_document_with_no_temporary_file_behind_it(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)

        self.build(builder)

        self.assertEqual(
            sorted(p.name for p in self.workspace.iterdir()), [IMAGE_LEDGER_FILENAME]
        )

    def test_a_readable_ledger_is_updated_in_place_and_never_set_aside_as_damaged(self):
        self.write_ledger(self.record_for(reference="ghcr.io/product-science/api:latest"))
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        builder = self.make_builder(runner)

        self.build(builder)

        self.assertEqual(self.corrupt_backups(), [])
        images = load_image_ledger(self.ledger)
        self.assertEqual(len(images), 2, "the pre-existing record was dropped")


class UnknownCreationTimeTests(LedgerTestCase):
    """An unreadable creation time proves nothing and may not be a free pass."""

    def test_an_image_with_an_unreadable_creation_time_now_requires_ledger_proof(self):
        builder = self.make_builder(UnreadableCreationTimeRunner())
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(
            cm.exception.details["reason"], "creation time unknown and no record of this image id"
        )
        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "STALE_IMAGE_REUSED")
        self.assertIsNone(failure["details"]["image_created_epoch"])
        self.assertEqual(manifest.images, [])

    def test_an_image_with_an_unreadable_creation_time_is_accepted_when_the_ledger_proves_it(self):
        self.write_ledger(self.record_for(created_epoch=None))
        builder = self.make_builder(UnreadableCreationTimeRunner())

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertIs(manifest.images[0]["cache_hit"], True)
        self.assertEqual(
            ledger_fingerprints(manifest.images[0]["cache_proof"]), [self.identity]
        )
        self.assertIsNone(manifest.images[0]["created_epoch"])

    def test_an_undated_image_of_another_component_is_refused_even_with_a_ledger(self):
        """A record exists, but not for the component this build is producing.

        Round 10 reproduced "cannot be proven" with a builder that had no
        source fingerprint. Round 11 derives the identity from the step, the
        recipe, the adapter and the sources that step reads, so a builder
        always has one -- the honest way to have no proof is to have a record
        that belongs to a different component, which is what this build is.
        """
        self.write_ledger(self.record_for(created_epoch=None))
        builder = self.make_builder(UnreadableCreationTimeRunner())
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest, context=self.other_context)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(manifest.failures[-1]["code"], "STALE_IMAGE_REUSED")
        self.assertEqual(
            manifest.failures[-1]["details"]["expected_component_identity"],
            self.other_identity,
        )

    def test_an_accepted_undated_image_is_remembered_with_no_creation_time(self):
        self.write_ledger(self.record_for(created_epoch=None))
        builder = self.make_builder(UnreadableCreationTimeRunner())

        self.build(builder)

        self.assertIsNone(self.ledger_entry()["created_epoch"])
        self.assertEqual(self.ledger_entry()["source_fingerprints"], [self.identity])

    def test_an_undated_image_recorded_for_other_sources_says_so_instead_of_no_record(self):
        """The reason must not contradict the details printed beside it."""
        self.write_ledger(
            self.record_for(created_epoch=None, fingerprints=[self.other_identity])
        )
        builder = self.make_builder(UnreadableCreationTimeRunner())
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertEqual(
            cm.exception.details["reason"],
            "creation time unknown and recorded against different sources",
        )
        details = manifest.failures[-1]["details"]
        self.assertEqual(details["recorded_source_fingerprints"], [self.other_identity])
        self.assertEqual(details["reason"], cm.exception.details["reason"])



class CachedImageAcceptanceTests(LedgerTestCase):
    """The bug fix: a proven Docker cache hit is a pass, not a failure."""

    def old_image_builder(self, **kwargs):
        runner = FakeBuildRunner(image_created_epoch=OLD_IMAGE_EPOCH)
        return self.make_builder(runner, **kwargs)

    def test_old_image_recorded_against_the_same_sources_is_accepted_as_a_cache_hit(self):
        self.write_ledger(self.record_for())
        builder = self.old_image_builder()

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertNotEqual(manifest.status, ManifestStatus.FAILED)
        self.assertEqual(len(manifest.images), 1)
        self.assertIs(manifest.images[0]["cache_hit"], True)

    def test_the_accepted_cache_hit_carries_the_ledger_record_as_its_proof(self):
        self.write_ledger(self.record_for())
        builder = self.old_image_builder()

        manifest = self.build(builder)

        proof = manifest.images[0]["cache_proof"]
        self.assertIsInstance(proof, dict)
        self.assertIn(self.identity, proof["source_fingerprints"])
        self.assertEqual(proof["image_id"], self.image_id())
        self.assertEqual(proof["reference"], CHAIN_IMAGE)
        self.assertEqual(proof["produced_by"], [f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}"])

    def test_an_image_recorded_for_several_identities_is_accepted_for_each_of_them(self):
        self.write_ledger(
            self.record_for(fingerprints=[self.identity, self.other_identity])
        )

        first = self.build(self.old_image_builder())
        second = self.build(self.old_image_builder(), context=self.other_context)

        self.assertEqual(first.failures, [])
        self.assertEqual(second.failures, [])
        self.assertIs(first.images[0]["cache_hit"], True)
        self.assertIs(second.images[0]["cache_hit"], True)

    def test_a_ledger_written_before_identities_were_a_list_still_proves_a_cache_hit(self):
        self.write_ledger(self.legacy_record_for())
        builder = self.old_image_builder()

        manifest = self.build(builder)

        self.assertEqual(manifest.failures, [])
        self.assertIs(manifest.images[0]["cache_hit"], True)

    def test_the_accepted_cache_hit_is_explained_to_the_operator(self):
        self.write_ledger(self.record_for())
        builder = self.old_image_builder()

        self.build(builder)

        self.assertTrue(
            any("Docker returned the cached image" in line for line in self.emitted),
            self.emitted,
        )
        self.assertTrue(
            any("built from these exact sources" in line for line in self.emitted),
            self.emitted,
        )

    def test_old_image_recorded_against_different_sources_is_refused_as_stale(self):
        self.write_ledger(self.record_for(fingerprints=[self.other_identity]))
        builder = self.old_image_builder()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "recorded against different sources")
        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "STALE_IMAGE_REUSED")
        self.assertEqual(failure["details"]["reason"], "recorded against different sources")
        self.assertEqual(
            failure["details"]["recorded_source_fingerprints"], [self.other_identity]
        )
        self.assertEqual(
            failure["details"]["expected_component_identity"], self.identity
        )
        # Round 11 renamed the expectation; the old key must not linger, or an
        # operator would read a run-level value as if it were the decision.
        self.assertNotIn("expected_source_fingerprint", failure["details"])
        self.assertEqual(failure["details"]["ledger"], str(self.ledger))
        self.assertEqual(manifest.images, [])

    def test_old_image_with_no_ledger_entry_at_all_is_still_refused(self):
        builder = self.old_image_builder()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "no record of this image id")
        failure = manifest.failures[-1]
        self.assertEqual(failure["code"], "STALE_IMAGE_REUSED")
        self.assertEqual(failure["details"]["recorded_source_fingerprints"], [])
        self.assertEqual(manifest.status, ManifestStatus.FAILED)

    def test_ledger_entry_for_a_different_image_id_does_not_authorise_this_image(self):
        self.write_ledger(self.record_for(image_id="sha256:" + "0" * 64))
        builder = self.old_image_builder()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "no record of this image id")

    def test_ledger_entry_for_another_reference_does_not_authorise_this_one(self):
        other = "ghcr.io/product-science/api:latest"
        # One image may have several tags. Change only its reference so an
        # image-ID-only lookup cannot accidentally satisfy this negative case.
        self.write_ledger(self.record_for(reference=other, image_id=self.image_id()))
        builder = self.old_image_builder()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(manifest.failures[-1]["details"]["reference"], CHAIN_IMAGE)

    def test_the_run_level_fingerprint_is_not_what_authorises_an_old_image(self):
        """Neither its value nor its absence moves the decision.

        Round 10 refused every cached image to a builder that had no
        ``source_fingerprint``, because that value *was* the proof. Round 11
        proves the image with the component identity, which this builder still
        has, so the record is honoured. The negative this test guards is the
        mirror image of the old one: the run-level fingerprint must not be
        consulted at all, so it cannot silently become a second gate.
        """
        self.write_ledger(self.record_for())
        proven = self.build(self.old_image_builder(fingerprint=None))
        other_run = self.build(self.old_image_builder(fingerprint=self.other_fingerprint))

        self.assertEqual(proven.failures, [])
        self.assertIs(proven.images[0]["cache_hit"], True)
        self.assertEqual(other_run.failures, [])
        self.assertIs(other_run.images[0]["cache_hit"], True)

    def test_an_old_image_of_a_component_the_ledger_never_saw_is_refused(self):
        """The refusal round 10 wanted, expressed the way round 11 decides."""
        self.write_ledger(self.record_for())
        builder = self.old_image_builder(fingerprint=None)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest, context=self.other_context)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "recorded against different sources")
        self.assertEqual(
            manifest.failures[-1]["details"]["expected_component_identity"],
            self.other_identity,
        )

    def test_corrupt_ledger_cannot_rescue_an_old_image_and_its_bytes_are_left_alone(self):
        damaged = "{ truncated"
        self.ledger.write_text(damaged, encoding="utf-8")
        builder = self.old_image_builder()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "no record of this image id")
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), damaged)

    def test_an_old_image_stops_the_run_before_any_later_image_is_recorded(self):
        builder = self.old_image_builder()
        manifest = new_manifest()
        # The first declared image is proven; the second one is not.
        recipe = self.chain_recipe(
            images=(CHAIN_IMAGE, "ghcr.io/product-science/api:latest")
        )
        # The identity covers the step, and the step declares its images, so
        # this recipe's identity is not the single-image recipe's identity.
        self.write_ledger(self.record_for(recipe=recipe))

        with self.assertRaises(BuildProvenanceError) as cm:
            self.build(builder, manifest=manifest, recipe=recipe)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual([entry["role"] for entry in manifest.images], [CHAIN_IMAGE])


class DamagedLedgerPreservationTests(LedgerTestCase):
    """A damaged ledger is evidence too: keep it, do not overwrite it."""

    @patch("ops.a8.e2e.builder.time.time", return_value=1234567890)
    def test_all_malformed_ledgers_are_preserved_when_recovered_in_the_same_second(self, _clock):
        self.build_with_a_fresh_image()
        original = self.ledger.read_text(encoding="utf-8")
        preserved_documents = []
        for field in ("run_fingerprints", "produced_by", "source_fingerprints"):
            for value in (42, "not a list", [{}], [1]):
                with self.subTest(field=field, value=value):
                    document = json.loads(original)
                    entry = next(iter(document["images"].values()))
                    entry[field] = value
                    damaged = json.dumps(document).encode("utf-8")
                    self.ledger.write_bytes(damaged)
                    preserved_documents.append(damaged)

                    self.assertTrue(ledger_is_damaged(self.ledger))
                    self.assertEqual(load_image_ledger(self.ledger), {})
                    manifest = self.build_with_a_fresh_image()

                    self.assertEqual(manifest.failures, [])
                    self.assertCountEqual(
                        [backup.read_bytes() for backup in self.corrupt_backups()],
                        preserved_documents,
                    )
                    self.assertFalse(ledger_is_damaged(self.ledger))
                    self.assertTrue(load_image_ledger(self.ledger))

    def test_a_backup_failure_keeps_the_damaged_original_and_warns_without_failing_the_build(self):
        self.build_with_a_fresh_image()
        document = json.loads(self.ledger.read_text(encoding="utf-8"))
        next(iter(document["images"].values()))["run_fingerprints"] = 42
        damaged = json.dumps(document).encode("utf-8")
        self.ledger.write_bytes(damaged)
        self.emitted.clear()

        with patch("ops.a8.e2e.builder.os.link", side_effect=PermissionError("backup denied")):
            manifest = self.build_with_a_fresh_image()

        self.assertEqual(manifest.failures, [])
        self.assertTrue(manifest.images)
        self.assertEqual(self.ledger.read_bytes(), damaged)
        self.assertEqual(self.corrupt_backups(), [])
        warnings = [line for line in self.emitted if "skipping ledger update" in line]
        self.assertEqual(len(warnings), 1, self.emitted)
        self.assertIn(str(self.ledger), warnings[0])
        self.assertFalse(self.ledger.with_name(self.ledger.name + ".tmp").exists())

    def test_invalid_utf8_in_a_produced_ledger_is_preserved_and_replaced_after_a_fresh_build(self):
        self.build_with_a_fresh_image()
        # Corrupt the actual producer's document by changing one byte.
        damaged = self.ledger.read_bytes().replace(b"{", b"\xff", 1)
        self.ledger.write_bytes(damaged)
        self.assertEqual(load_image_ledger(self.ledger), {})
        self.assertTrue(ledger_is_damaged(self.ledger))

        self.build_with_a_fresh_image()

        backups = self.corrupt_backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), damaged)
        self.assertFalse(ledger_is_damaged(self.ledger))
        self.assertTrue(load_image_ledger(self.ledger))

    def build_with_a_fresh_image(self):
        runner = FakeBuildRunner(image_created_epoch=FRESH_IMAGE_EPOCH)
        return self.build(self.make_builder(runner))

    def test_a_damaged_ledger_is_kept_aside_instead_of_being_overwritten(self):
        damaged = '{"schema": "a8.e2e.image-provenance/v1", "images": {trunc'
        self.ledger.write_text(damaged, encoding="utf-8")

        self.build_with_a_fresh_image()

        backups = self.corrupt_backups()
        self.assertEqual(len(backups), 1, self.corrupt_backups())
        self.assertEqual(backups[0].read_text(encoding="utf-8"), damaged)

    def test_the_run_continues_on_a_freshly_written_ledger_after_the_damage(self):
        self.ledger.write_text("not json", encoding="utf-8")

        manifest = self.build_with_a_fresh_image()

        self.assertEqual(manifest.failures, [])
        self.assertEqual(
            list(load_image_ledger(self.ledger)), [ledger_key(CHAIN_IMAGE, self.image_id())]
        )

    def test_the_operator_is_told_where_the_damaged_ledger_was_kept(self):
        self.ledger.write_text("not json", encoding="utf-8")

        self.build_with_a_fresh_image()

        warnings = [
            line for line in self.emitted
            if line.startswith("[build] warning: the image ledger was unreadable and was kept at")
        ]
        self.assertEqual(len(warnings), 1, self.emitted)
        self.assertIn(str(self.corrupt_backups()[0]), warnings[0])
        self.assertIn("will have to be rebuilt once", warnings[0])

    def test_damage_is_decided_by_reading_the_file_not_by_the_absence_of_images(self):
        """Absent, empty and corrupt are three different things."""
        self.assertFalse(ledger_is_damaged(None))
        self.assertFalse(ledger_is_damaged(self.ledger))  # not written yet
        self.write_ledger({})
        self.assertFalse(ledger_is_damaged(self.ledger))
        self.write_ledger(self.record_for())
        self.assertFalse(ledger_is_damaged(self.ledger))
        for bad in ('{"images": {trunc', "[]", '{"images": []}', '"a string"'):
            with self.subTest(document=bad):
                self.ledger.write_text(bad, encoding="utf-8")
                self.assertTrue(ledger_is_damaged(self.ledger))

    def test_a_valid_ledger_with_no_images_yet_is_never_treated_as_damage(self):
        """The very first build writes into exactly this file."""
        self.write_ledger({})
        original = self.ledger.read_text(encoding="utf-8")

        self.build_with_a_fresh_image()

        self.assertEqual(self.corrupt_backups(), [])
        self.assertEqual(
            [line for line in self.emitted if "unreadable" in line], []
        )
        self.assertNotEqual(self.ledger.read_text(encoding="utf-8"), original)
        self.assertIn(ledger_key(CHAIN_IMAGE, self.image_id()), load_image_ledger(self.ledger))



class LedgerRoundTripTests(LedgerTestCase):
    """Two real passes: the first build teaches the ledger, the second reuses.

    Round 11 (finding F2) changed what "the same sources" means for a cached
    image: the decision is made on the *step's* component identity, not on the
    run-level fingerprint handed to the ``Builder``. The variants below
    therefore change the build context the identity is derived from. The
    round-trip claim itself -- teach once, reuse afterwards, refuse everything
    that was not taught -- is exactly the one round 10 made.
    """

    def pass_with(
        self,
        *,
        context=None,
        fingerprint=...,
        ledger_path=...,
        started_epoch=BUILD_STARTED_EPOCH,
        created_epoch=FRESH_IMAGE_EPOCH,
        recipe=None,
        manifest=None,
    ):
        runner = FakeBuildRunner(image_created_epoch=created_epoch)
        builder = self.make_builder(
            runner, fingerprint=fingerprint, ledger_path=ledger_path
        )
        return self.build(
            builder,
            started_epoch=started_epoch,
            recipe=recipe,
            manifest=manifest,
            context=context,
        )

    def first_pass(self, **kwargs):
        manifest = self.pass_with(**kwargs)
        self.assertIs(manifest.images[0]["cache_hit"], False)
        return manifest

    def rebuild_pass(self, *, context=None, recipe=None):
        """Recreate the same immutable image under a newly proven identity."""
        daemon = FakeDockerDaemon(images={CHAIN_IMAGE: {
            "id": self.image_id(), "created_epoch": FRESH_IMAGE_EPOCH,
        }})
        manifest = self.build(
            self.make_builder(daemon), started_epoch=LATER_BUILD_EPOCH,
            context=context, recipe=recipe,
        )
        self.assertEqual(daemon.removals, [CHAIN_IMAGE])
        self.assertEqual(len(daemon.build_invocations), 2)
        image = manifest.images[0]
        self.assertEqual(image["image_id"], self.image_id())
        self.assertEqual(image["created_epoch"], FRESH_IMAGE_EPOCH)
        self.assertEqual(image["cache_proof"]["kind"], "REBUILT_AFTER_REMOVAL")
        self.assertEqual(
            image["cache_proof"]["component_identity"],
            self.identity_for(context=context, recipe=recipe),
        )
        return manifest

    def test_a_second_build_of_the_same_sources_accepts_the_image_the_first_recorded(self):
        self.first_pass()

        second = self.pass_with(started_epoch=LATER_BUILD_EPOCH)

        self.assertEqual(second.failures, [])
        self.assertIs(second.images[0]["cache_hit"], True)
        self.assertEqual(second.images[0]["image_id"], self.image_id())
        self.assertIn(self.identity, second.images[0]["cache_proof"]["source_fingerprints"])

    def test_a_second_build_of_different_sources_refuses_the_recorded_image(self):
        """One changed input -- the commit that is built -- and it is refused."""
        self.first_pass()
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.pass_with(
                context=self.other_context,
                started_epoch=LATER_BUILD_EPOCH,
                manifest=manifest,
            )

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "recorded against different sources")
        self.assertEqual(
            manifest.failures[-1]["details"]["recorded_source_fingerprints"], [self.identity]
        )
        self.assertEqual(
            manifest.failures[-1]["details"]["expected_component_identity"],
            self.other_identity,
        )

    def test_a_first_pass_that_recorded_nothing_teaches_nothing(self):
        """A build that kept no ledger cannot authorise the next one.

        Round 10 produced this state with a builder that had no source
        fingerprint. Round 11 derives the identity from the step itself, so a
        builder always has one; the only way to record nothing is to have
        nowhere to record it, which is the state reproduced here.
        """
        self.first_pass(ledger_path=None)
        manifest = new_manifest()

        with self.assertRaises(BuildProvenanceError) as cm:
            self.pass_with(started_epoch=LATER_BUILD_EPOCH, manifest=manifest)

        self.assertIn("Refusing a stale cached image", str(cm.exception))
        self.assertEqual(cm.exception.details["reason"], "no record of this image id")

    def test_the_ledger_keeps_one_entry_per_image_across_repeated_builds(self):
        self.first_pass()
        self.pass_with(started_epoch=LATER_BUILD_EPOCH)
        self.pass_with(started_epoch=LATER_BUILD_EPOCH)

        images = load_image_ledger(self.ledger)
        self.assertEqual(list(images), [ledger_key(CHAIN_IMAGE, self.image_id())])
        self.assertEqual(ledger_fingerprints(self.ledger_entry()), [self.identity])

    def test_two_identities_that_produce_the_same_image_are_both_remembered(self):
        self.first_pass()

        # The cached image keeps its original Created value; removal and
        # recreation establish provenance for the second component identity.
        self.rebuild_pass(context=self.other_context)

        self.assertEqual(
            ledger_fingerprints(self.ledger_entry()),
            sorted([self.identity, self.other_identity]),
        )

    def test_an_identity_recorded_first_still_proves_a_cache_hit_after_a_second_one(self):
        self.first_pass()
        self.rebuild_pass(context=self.other_context)

        later = self.pass_with(started_epoch=LATER_BUILD_EPOCH + 7200.0)

        self.assertEqual(later.failures, [])
        self.assertIs(later.images[0]["cache_hit"], True)

    def test_the_first_recording_time_does_not_drift_when_the_image_is_reused(self):
        first_time = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        second_time = first_time + dt.timedelta(hours=1)
        # Control the clock boundary, keeping serialization and ledger updates
        # real. A cached timestamp cannot pass by equality within one instant.
        with patch("ops.a8.e2e.runlock.dt", wraps=dt) as clock:
            clock.datetime.now.return_value = first_time
            self.first_pass()
            self.assertEqual(self.ledger_entry()["first_recorded_at_utc"], first_time.isoformat())
            clock.datetime.now.return_value = second_time
            self.pass_with(started_epoch=LATER_BUILD_EPOCH)

        entry = self.ledger_entry()
        self.assertEqual(entry["first_recorded_at_utc"], first_time.isoformat())
        self.assertEqual(entry["last_recorded_at_utc"], second_time.isoformat())

    def test_different_runs_of_one_component_accumulate_run_fingerprints_without_duplicates(self):
        self.first_pass()
        for _ in range(2):
            manifest = self.pass_with(
                started_epoch=LATER_BUILD_EPOCH, fingerprint=self.other_fingerprint,
            )
            self.assertIs(manifest.images[0]["cache_hit"], True)
            self.assertEqual(manifest.images[0]["component_identity"], self.identity)
            entry = self.ledger_entry()
            self.assertEqual(entry["source_fingerprints"], [self.identity])
            self.assertEqual(
                entry["run_fingerprints"], sorted([self.fingerprint, self.other_fingerprint]),
            )

    def test_each_recipe_and_step_that_produced_the_image_is_listed_once(self):
        """``produced_by`` accumulates; it is a record, not a cache decision.

        The legacy recipe's step has its own component identity, so its pass
        requires removal and recreation to establish its own provenance. The
        image itself still retains its original ID and creation timestamp.
        """
        self.first_pass()

        self.rebuild_pass(recipe=self.legacy_chain_recipe())
        self.pass_with(started_epoch=LATER_BUILD_EPOCH)

        self.assertEqual(
            self.ledger_entry()["produced_by"],
            sorted(
                {
                    f"{UPSTREAM_RECIPE_ID}:{CHAIN_STEP_ID}",
                    f"{LEGACY_RECIPE_ID}:{CHAIN_STEP_ID}",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
