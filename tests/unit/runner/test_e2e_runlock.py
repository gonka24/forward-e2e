"""Unit tests for ``forward_e2e.execution.runlock``: the lock envelope and its hash, lock
immutability, the recorded source provenance and the post-build manifests.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from forward_e2e.execution.errors import (
    LockIntegrityError,
    LockSchemaError,
    PathSafetyError,
    PlanSchemaSupersededError,
    SourceSpecError,
)
from forward_e2e.execution.gitio import validate_remote_url
from forward_e2e.execution.file_safety import atomic_publish_file
from forward_e2e.execution.planner import _source_policy_section
from forward_e2e.execution.runlock import (
    BUILD_MANIFEST_SCHEMA,
    EXECUTION_MANIFEST_SCHEMA,
    LOCK_ENVELOPE_SCHEMA,
    PROVENANCE_HISTORICAL_PREPARED,
    PROVENANCE_IMMUTABLE,
    RUN_LOCK_FILENAME,
    RUN_LOCK_SCHEMA,
    RUN_LOCK_SCHEMA_V1,
    SEMANTIC_LOCK_SECTIONS,
    SUPERSEDED_LOCK_KEYS,
    BuildManifest,
    DeliveryManifest,
    ExecutionManifest,
    ManifestStatus,
    RunLock,
    assert_lock_executable,
    assert_manifest_matches_lock,
    canonical_json,
    content_sha256,
    copy_lock_into_run,
    load_run_lock,
    preserve_source_bundles,
    superseded_lock_markers,
    write_run_lock,
)
from forward_e2e.execution.sources import (
    AcquiredSource,
    SourceKind,
    SourceSpec,
    SubmoduleRecord,
    bundle_ref,
    github_commit_url,
)

GONKA_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"
CONTRACTS_SHA = "89abcdef0123456789abcdef0123456789abcdef"

GONKA_ORIGIN = "https://github.com/gonka-ai/gonka.git"


def _source_section(role, sha, *, repo_url=None, local_path=None, origin=None):
    """Build a lock source section exactly the way the planner does."""
    spec = SourceSpec(
        role=role,
        kind=SourceKind.REMOTE if repo_url else SourceKind.LOCAL,
        commit_sha=sha,
        repo_url=repo_url,
        local_path=local_path,
    )
    acquired = AcquiredSource(
        spec=spec,
        worktree=Path("/workspace/src") / role,
        bundle_path=Path("/workspace/package/bundles") / f"{role}.bundle",
        bundle_relpath=f"bundles/{role}.bundle",
        bundle_sha256=hashlib.sha256(f"{role}-bundle".encode("utf-8")).hexdigest(),
        bundle_ref=bundle_ref(role),
        commit_url=github_commit_url(origin, sha),
        resolved_origin_url=origin,
    )
    return acquired.to_lock_record()


def _make_lock(**overrides):
    payload = {
        "schema_version": RUN_LOCK_SCHEMA,
        "plan_id": "plan-20240101-000000-abcdef",
        "created_at_utc": "2024-01-01T00:00:00+00:00",
        "gonka": _source_section(
            "gonka", GONKA_SHA, repo_url=GONKA_ORIGIN, origin=GONKA_ORIGIN
        ),
        "contracts": _source_section(
            "contracts", CONTRACTS_SHA, local_path="/mnt/contracts", origin=None
        ),
        "runner": {"image_id": "sha256:" + "1" * 64, "image_tag": "a8-e2e:local"},
        "platform": {"os": "linux", "arch": "amd64"},
        "compatibility": {
            "gonka": {"adapter_id": "gonka-v1", "match_mode": "exact"},
            "contracts": {"adapter_id": "contracts-v1", "match_mode": "exact"},
        },
        "selection": {"profile": "native", "scenarios": ["lock-exact-e", "funded-claim"]},
        "limits": {"per_task_timeout_seconds": 900},
        "network": {"chain_id": "gonka-e2e", "fresh_state": True},
        "build": {"gonka": {"targets": ["inferenced"]}, "contracts": {"targets": ["marketplace"]}},
        "source_policy": _source_policy_section(),
        "external_tests": {"trees": {}, "tests_hash": "a" * 64},
        "semantic_inputs": {"E2E_PROOF_LEVEL": "native"},
        "source_package": {"bundles": ["bundles/gonka.bundle", "bundles/contracts.bundle"]},
        "notes": ["synthetic fixture"],
    }
    payload.update(overrides)
    return RunLock(**payload)


class RunLockEnvelopeTests(unittest.TestCase):
    """The envelope binds a hash to a body it does not live inside."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-lock-")
        self.root = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_the_hash_is_taken_over_the_inner_body_and_stored_outside_of_it(self):
        lock = _make_lock()
        envelope = lock.envelope()

        self.assertEqual(set(envelope), {"envelope_schema", "lock_sha256", "lock"})
        self.assertEqual(envelope["envelope_schema"], LOCK_ENVELOPE_SCHEMA)
        self.assertNotIn("lock_sha256", envelope["lock"])

        recomputed = hashlib.sha256(
            canonical_json(envelope["lock"]).encode("utf-8")
        ).hexdigest()
        self.assertEqual(recomputed, envelope["lock_sha256"])
        self.assertEqual(content_sha256(envelope["lock"]), envelope["lock_sha256"])
        self.assertEqual(lock.lock_sha256, envelope["lock_sha256"])
        self.assertEqual(len(envelope["lock_sha256"]), 64)

    def test_mutating_any_field_of_the_lock_body_changes_the_recorded_hash(self):
        lock = _make_lock()
        original_hash = lock.lock_sha256
        body = lock.to_dict()

        mutations = {
            "selection": {**body["selection"], "scenarios": ["lock-exact-e"]},
            "limits": {**body["limits"], "per_task_timeout_seconds": 901},
            "gonka": {**body["gonka"], "commit_sha": CONTRACTS_SHA},
            "runner": {**body["runner"], "image_id": "sha256:" + "2" * 64},
        }
        for section, replacement in mutations.items():
            with self.subTest(section=section):
                mutated = copy.deepcopy(body)
                mutated[section] = replacement
                self.assertNotEqual(content_sha256(mutated), original_hash)

    def test_a_manifest_bound_to_the_old_hash_no_longer_matches_a_mutated_lock(self):
        lock = _make_lock()
        manifest = BuildManifest.start(lock=lock, run_id="run-0001")
        assert_manifest_matches_lock(manifest, lock)

        lock.selection = {**lock.selection, "scenarios": ["lock-exact-e"]}

        with self.assertRaises(LockIntegrityError) as cm:
            assert_manifest_matches_lock(manifest, lock)
        self.assertIn("belongs to a different lock", str(cm.exception))

    def test_a_lock_edited_on_disk_is_refused_when_it_is_loaded_back(self):
        lock = _make_lock()
        path = write_run_lock(lock, self.root / "plan" / RUN_LOCK_FILENAME)
        self.assertEqual(load_run_lock(path).to_dict(), lock.to_dict())

        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["lock"]["selection"]["scenarios"] = ["funded-claim"]
        path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaises(LockIntegrityError) as cm:
            load_run_lock(path)
        self.assertIn("modified after it was created", str(cm.exception))

    def test_an_existing_lock_is_never_rewritten(self):
        lock = _make_lock()
        path = write_run_lock(lock, self.root / "plan" / RUN_LOCK_FILENAME)

        with self.assertRaises(LockIntegrityError) as cm:
            write_run_lock(lock, path)
        self.assertIn("a lock is never rewritten", str(cm.exception))

    def test_an_unknown_envelope_schema_is_refused(self):
        lock = _make_lock()
        envelope = lock.envelope()
        envelope["envelope_schema"] = "e2e/run-lock-envelope/99"
        path = self.root / RUN_LOCK_FILENAME
        path.write_text(json.dumps(envelope), encoding="utf-8")

        with self.assertRaises(LockSchemaError) as cm:
            load_run_lock(path)
        self.assertIn("Unsupported run lock envelope schema", str(cm.exception))

    def test_an_unknown_lock_body_schema_is_refused(self):
        body = _make_lock().to_dict()
        body["schema_version"] = "e2e/run-lock/99"

        with self.assertRaises(LockSchemaError) as cm:
            RunLock.from_dict(body)
        self.assertIn("Unsupported run lock schema version", str(cm.exception))

    def test_the_preserved_copy_of_a_lock_is_byte_identical_and_never_overwritten(self):
        lock = _make_lock()
        path = write_run_lock(lock, self.root / "plan" / RUN_LOCK_FILENAME)
        run_dir = self.root / "runs" / "run-0001"

        preserved = copy_lock_into_run(path, run_dir)
        self.assertEqual(preserved.read_bytes(), path.read_bytes())

        with self.assertRaises(LockIntegrityError) as cm:
            copy_lock_into_run(path, run_dir)
        self.assertIn("Refusing to overwrite the lock already preserved", str(cm.exception))


class RunLockSourceProvenanceTests(unittest.TestCase):
    """What the lock says about "which code" was pinned."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-lock-")
        self.root = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_github_origin_yields_a_clickable_commit_link_for_that_exact_sha(self):
        lock = _make_lock()
        record = lock.source_record("gonka")

        self.assertEqual(record["commit_sha"], GONKA_SHA)
        self.assertEqual(record["source_kind"], "remote")
        self.assertEqual(record["repo_url"], GONKA_ORIGIN)
        self.assertEqual(
            record["commit_url"],
            f"https://github.com/gonka-ai/gonka/commit/{GONKA_SHA}",
        )
        self.assertEqual(record["bundle_ref"], "refs/e2e/source/gonka")

    def test_an_unknown_origin_records_a_null_commit_link_instead_of_inventing_one(self):
        lock = _make_lock()
        record = lock.source_record("contracts")

        self.assertEqual(record["commit_sha"], CONTRACTS_SHA)
        self.assertEqual(record["source_kind"], "local")
        self.assertIsNone(record["repo_url"])
        self.assertIsNone(record["resolved_origin_url"])
        self.assertIsNone(record["commit_url"])

        serialised = json.dumps(lock.envelope(), indent=2, sort_keys=True)
        self.assertIn('"commit_url": null', serialised)

    def test_an_unknown_source_role_is_refused(self):
        lock = _make_lock()
        with self.assertRaises(LockSchemaError) as cm:
            lock.source_record("runner")
        self.assertIn("Unknown source role", str(cm.exception))

    def test_a_credential_value_can_never_reach_the_serialised_lock(self):
        token = "ghp_" + "C" * 32

        # A credential bearing URL cannot even become a source spec, so it can
        # never be recorded as the lock's repo_url in the first place.
        with self.assertRaises(SourceSpecError) as cm:
            validate_remote_url(f"https://x-access-token:{token}@github.com/gonka-ai/gonka.git")
        self.assertIn("must not embed credentials", str(cm.exception))
        self.assertNotIn(token, str(cm.exception))

        lock = _make_lock()
        serialised = json.dumps(lock.envelope(), indent=2, sort_keys=True)
        self.assertNotIn(token, serialised)
        self.assertNotIn("x-access-token", serialised)
        self.assertIn(GONKA_ORIGIN, serialised)


class SemanticLockSectionTests(unittest.TestCase):
    """The sections that define what a replay is allowed to reproduce."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-lock-")
        self.root = Path(self.tmp_dir.name).resolve()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_the_semantic_sections_are_exactly_the_meaning_bearing_sections(self):
        self.assertEqual(
            SEMANTIC_LOCK_SECTIONS,
            (
                "gonka",
                "contracts",
                "runner",
                "platform",
                "compatibility",
                "selection",
                "limits",
                "network",
                "build",
                "source_policy",
                "external_tests",
                "semantic_inputs",
                "source_package",
            ),
        )

    def test_every_semantic_section_exists_on_a_constructed_lock(self):
        body = _make_lock().to_dict()
        for section in SEMANTIC_LOCK_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, body)
                self.assertIsInstance(body[section], dict)

    def test_operational_fields_are_deliberately_not_semantic(self):
        for field_name in ("plan_id", "created_at_utc", "notes"):
            with self.subTest(field=field_name):
                self.assertNotIn(field_name, SEMANTIC_LOCK_SECTIONS)

    def test_a_lock_body_missing_a_semantic_section_cannot_be_loaded(self):
        body = _make_lock().to_dict()
        del body["network"]

        with self.assertRaises(LockSchemaError) as cm:
            RunLock.from_dict(body)
        self.assertIn("missing required sections", str(cm.exception))
        self.assertIn("network", str(cm.exception))

    def test_historical_v1_lock_remains_readable_and_classified_as_historical_but_is_not_executable(self):
        v1_lock = _make_lock(
            schema_version=RUN_LOCK_SCHEMA_V1,
            overlay={"entries": []},
        )
        path = write_run_lock(v1_lock, self.root / "v1" / RUN_LOCK_FILENAME)
        loaded = load_run_lock(path)
        self.assertEqual(loaded.schema_version, RUN_LOCK_SCHEMA_V1)
        self.assertEqual(loaded.provenance_model, PROVENANCE_HISTORICAL_PREPARED)
        with self.assertRaises(PlanSchemaSupersededError) as caught:
            assert_lock_executable(loaded)
        self.assertEqual(caught.exception.code, "PLAN_SCHEMA_SUPERSEDED")

    def test_a_v2_lock_with_any_superseded_marker_key_is_refused_for_execution_and_not_classified_immutable(self):
        clean = _make_lock()
        self.assertEqual(clean.provenance_model, PROVENANCE_IMMUTABLE)
        assert_lock_executable(clean)

        for key in SUPERSEDED_LOCK_KEYS:
            with self.subTest(superseded_key=key):
                tainted = _make_lock()
                tainted.gonka[f"test_{key}"] = "present"
                markers = superseded_lock_markers(tainted.to_dict())
                self.assertIn(f"lock.gonka.test_{key}", markers)
                self.assertEqual(tainted.provenance_model, PROVENANCE_HISTORICAL_PREPARED)
                with self.assertRaises(PlanSchemaSupersededError) as caught:
                    assert_lock_executable(tainted)
                self.assertIn(f"lock.gonka.test_{key}", caught.exception.details["superseded_keys"])


class BuildManifestTests(unittest.TestCase):
    """A crashed run must never be able to masquerade as a finished build."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-lock-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.lock = _make_lock()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_a_started_manifest_is_bound_to_its_lock_and_is_not_yet_finished(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")

        self.assertEqual(manifest.lock_sha256, self.lock.lock_sha256)
        self.assertEqual(manifest.plan_id, self.lock.plan_id)
        self.assertEqual(manifest.schema_version, BUILD_MANIFEST_SCHEMA)
        self.assertEqual(manifest.status, ManifestStatus.IN_PROGRESS)
        self.assertEqual(manifest.status, "IN_PROGRESS")
        self.assertIsNone(manifest.completed_at_utc)
        assert_manifest_matches_lock(manifest, self.lock)

    def test_a_recorded_failure_prevents_the_manifest_from_ever_finishing_complete(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")
        manifest.images.append({"role": "gonka", "image_id": "sha256:" + "3" * 64})
        manifest.record_failure(
            "BUILD_FAILED", "contracts build failed", {"exit_code": 1}
        )

        self.assertEqual(manifest.status, ManifestStatus.FAILED)

        manifest.finish(required_roles=("gonka",))

        self.assertEqual(manifest.status, ManifestStatus.FAILED)
        self.assertEqual(manifest.status, "FAILED")
        self.assertNotEqual(manifest.status, ManifestStatus.COMPLETE)
        self.assertIsNotNone(manifest.completed_at_utc)
        self.assertEqual(manifest.failures[0]["code"], "BUILD_FAILED")
        self.assertEqual(manifest.to_dict()["status"], "FAILED")

    def test_a_missing_required_artefact_finishes_partial_and_records_why(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")
        manifest.images.append({"role": "gonka", "image_id": "sha256:" + "3" * 64})

        manifest.finish(required_roles=("gonka", "contracts"))

        self.assertEqual(manifest.status, ManifestStatus.PARTIAL)
        self.assertEqual(manifest.status, "PARTIAL")
        self.assertEqual(manifest.failures[0]["code"], "BUILD_ARTIFACT_MISSING")
        self.assertEqual(manifest.failures[0]["details"]["missing_roles"], ["contracts"])

    def test_complete_is_reached_only_when_every_required_role_was_produced(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")
        manifest.images.append({"role": "gonka", "image_id": "sha256:" + "3" * 64})
        manifest.wasm.append({"role": "contracts", "sha256": "4" * 64})

        manifest.finish(required_roles=("gonka", "contracts"))

        self.assertEqual(manifest.status, ManifestStatus.COMPLETE)
        self.assertEqual(manifest.status, "COMPLETE")
        self.assertEqual(manifest.failures, [])

    def test_a_manifest_round_trips_through_disk_without_losing_its_lock_binding(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")
        manifest.images.append({"role": "gonka", "image_id": "sha256:" + "3" * 64})
        manifest.finish(required_roles=("gonka",))
        path = manifest.write(self.root / "runs" / "run-0001" / "build-manifest.json")

        reloaded = BuildManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

        self.assertEqual(reloaded.to_dict(), manifest.to_dict())
        assert_manifest_matches_lock(reloaded, self.lock)

    def test_a_manifest_with_an_unsupported_schema_is_refused(self):
        manifest = BuildManifest.start(lock=self.lock, run_id="run-0001")
        body = manifest.to_dict()
        body["schema_version"] = "e2e/build-manifest/99"

        with self.assertRaises(LockSchemaError) as cm:
            BuildManifest.from_dict(body)
        self.assertIn("Unsupported build manifest schema", str(cm.exception))


class ExecutionManifestTests(unittest.TestCase):
    """Run identity, including the parentage of a replay."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="a8-test-e2e-lock-")
        self.root = Path(self.tmp_dir.name).resolve()
        self.lock = _make_lock()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _manifest(self):
        return ExecutionManifest(
            schema_version=EXECUTION_MANIFEST_SCHEMA,
            lock_sha256=self.lock.lock_sha256,
            plan_id=self.lock.plan_id,
            run_id="run-0002",
            suite_id="suite-native",
            created_at_utc="2024-01-02T03:04:05+00:00",
            command="run",
            replay_of_plan=self.lock.plan_id,
            parent_run_id="run-0001",
            source_plan_path="/workspace/plan/run.lock.json",
            artifact_index_relpath="artifacts/index.json",
            runner_image_id="sha256:" + "1" * 64,
        )

    def test_a_replay_records_the_plan_it_reproduces_and_the_run_it_descends_from(self):
        manifest = self._manifest()
        body = manifest.to_dict()

        self.assertEqual(body["replay_of_plan"], self.lock.plan_id)
        self.assertEqual(body["parent_run_id"], "run-0001")
        self.assertEqual(body["lock_sha256"], self.lock.lock_sha256)
        self.assertTrue(body["fresh_network_state"])

    def test_an_execution_manifest_round_trips_through_disk_unchanged(self):
        manifest = self._manifest()
        path = manifest.write(self.root / "runs" / "run-0002" / "execution-manifest.json")

        reloaded = ExecutionManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

        self.assertEqual(reloaded.to_dict(), manifest.to_dict())
        self.assertEqual(reloaded.replay_of_plan, self.lock.plan_id)
        self.assertEqual(reloaded.parent_run_id, "run-0001")

    def test_a_first_run_records_no_parent_and_no_replayed_plan(self):
        manifest = self._manifest()
        manifest.replay_of_plan = None
        manifest.parent_run_id = None

        body = manifest.to_dict()
        self.assertIsNone(body["replay_of_plan"])
        self.assertIsNone(body["parent_run_id"])

    def test_an_execution_manifest_with_an_unsupported_schema_is_refused(self):
        body = self._manifest().to_dict()
        body["schema_version"] = "e2e/execution-manifest/99"

        with self.assertRaises(LockSchemaError) as cm:
            ExecutionManifest.from_dict(body)
        self.assertIn("Unsupported execution manifest schema", str(cm.exception))


class RunLockPublicationTests(unittest.TestCase):
    def test_locks_remain_readable_by_host_users_after_creation_copying_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = write_run_lock(_make_lock(), root / "plan" / RUN_LOCK_FILENAME)
            copied = copy_lock_into_run(source, root / "run")
            exported = atomic_publish_file(copied, root / "export" / RUN_LOCK_FILENAME)
            for path in (source, copied, exported):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                self.assertEqual(path.read_bytes(), source.read_bytes())

    def test_manifests_are_readable_on_creation_and_retain_permissions_on_update_and_export(self):
        lock = _make_lock()
        manifests = [
            BuildManifest.start(lock=lock, run_id="run-1"),
            ExecutionManifest(
                schema_version=EXECUTION_MANIFEST_SCHEMA, lock_sha256=lock.lock_sha256,
                plan_id=lock.plan_id, run_id="run-1", suite_id="suite-native",
                created_at_utc=lock.created_at_utc, command="run",
            ),
            DeliveryManifest(run_id="run-1"),
        ]
        for manifest in manifests:
            with self.subTest(manifest=type(manifest).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                target = manifest.write(root / "manifest.json")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                for mode in (0o644, 0o640):
                    with self.subTest(mode=oct(mode)):
                        target.chmod(mode)
                        manifest.run_id = f"run-{mode}"
                        manifest.write(target)
                        exported = atomic_publish_file(target, root / "export.json")
                        for path in (target, exported):
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                            self.assertEqual(json.loads(path.read_bytes()), manifest.to_dict())

    def test_lock_loading_refuses_file_directory_and_parent_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            lock = _make_lock()
            source = write_run_lock(lock, root / "plan" / RUN_LOCK_FILENAME)
            file_link = root / "lock-link.json"
            file_link.symlink_to(source)
            directory_link = root / "plan-link"
            directory_link.symlink_to(source.parent, target_is_directory=True)
            linked_package = root / "linked-package"
            linked_package.mkdir()
            (linked_package / RUN_LOCK_FILENAME).symlink_to(source)
            for path in (file_link, directory_link, directory_link / RUN_LOCK_FILENAME, linked_package):
                with self.subTest(path=path), self.assertRaises(PathSafetyError):
                    load_run_lock(path)
            self.assertEqual(load_run_lock(source).lock_sha256, lock.lock_sha256)
            self.assertEqual(load_run_lock(source.parent).lock_sha256, lock.lock_sha256)

    def test_concurrent_lock_writers_publish_exactly_one_complete_document(self):
        for mode in ("write", "copy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                locks = [_make_lock(plan_id=f"plan-{i}") for i in range(2)]
                sources = [write_run_lock(lock, root / f"source-{i}.json")
                           for i, lock in enumerate(locks)]
                destination = root / "run" / RUN_LOCK_FILENAME
                barrier = threading.Barrier(2)
                original_link = os.link

                def publish(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original_link(*args, **kwargs)

                def write(index):
                    try:
                        if mode == "write":
                            write_run_lock(locks[index], destination)
                        else:
                            copy_lock_into_run(sources[index], destination.parent)
                    except LockIntegrityError:
                        return None
                    return index

                with patch("forward_e2e.execution.file_safety.os.link", side_effect=publish):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(write, range(2)))
                winners = [index for index in results if index is not None]
                self.assertEqual(len(winners), 1)
                self.assertEqual(destination.read_bytes(), sources[winners[0]].read_bytes())
                self.assertEqual(load_run_lock(destination).lock_sha256,
                                 locks[winners[0]].lock_sha256)
                self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_a_dangling_lock_destination_symlink_cannot_redirect_either_writer(self):
        for mode in ("write", "copy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                lock = _make_lock()
                source = write_run_lock(lock, root / "source.json")
                victim = root / "absent.json"
                destination = root / RUN_LOCK_FILENAME
                destination.symlink_to(victim)
                with self.assertRaises(PathSafetyError):
                    if mode == "write":
                        write_run_lock(lock, destination)
                    else:
                        copy_lock_into_run(source, root)
                self.assertFalse(victim.exists())
                self.assertTrue(destination.is_symlink())

    def test_invalid_utf8_in_a_lock_is_reported_as_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = _make_lock(notes=["readable-note"])
            path = write_run_lock(lock, Path(tmp).resolve() / RUN_LOCK_FILENAME)
            self.assertEqual(load_run_lock(path).lock_sha256, lock.lock_sha256)
            path.write_bytes(path.read_bytes().replace(b"readable-note", b"\xff"))

            with self.assertRaises(LockSchemaError) as caught:
                load_run_lock(path)

            self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)
            self.assertEqual(caught.exception.details["path"], str(path))

    def test_an_unpaired_surrogate_in_a_lock_is_reported_as_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = _make_lock(notes=["Unicode note: \u041f\u0440\u0438\u0432\u0435\u0442 \U0001f680"])
            path = write_run_lock(lock, Path(tmp).resolve() / RUN_LOCK_FILENAME)
            self.assertEqual(load_run_lock(path).lock_sha256, lock.lock_sha256)
            envelope = lock.envelope()
            envelope["lock"]["notes"][0] = "\ud800"
            path.write_text(json.dumps(envelope), encoding="utf-8")

            with self.assertRaises(LockSchemaError) as caught:
                load_run_lock(path)

            self.assertIsInstance(caught.exception.__cause__, UnicodeEncodeError)
            self.assertEqual(caught.exception.details["path"], str(path))

    def test_a_verified_body_that_changes_when_deserialized_is_rejected(self):
        for change in ("missing_notes", "extra_field", "numeric_plan_id"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                envelope = _make_lock().envelope()
                body = envelope["lock"]
                if change == "missing_notes":
                    del body["notes"]
                elif change == "extra_field":
                    body["unknown"] = "value"
                else:
                    body["plan_id"] = 123
                envelope["lock_sha256"] = content_sha256(body)
                target = Path(tmp) / RUN_LOCK_FILENAME
                target.write_text(json.dumps(envelope), encoding="utf-8")
                with self.assertRaises(LockSchemaError):
                    load_run_lock(target)

    def test_failed_manifest_publication_preserves_the_previous_complete_document(self):
        lock = _make_lock()
        manifests = [
            BuildManifest.start(lock=lock, run_id="run-1"),
            ExecutionManifest(EXECUTION_MANIFEST_SCHEMA, lock.lock_sha256,
                              lock.plan_id, "run-1", "suite-native",
                              lock.created_at_utc, "run"),
        ]
        for manifest in manifests:
            with self.subTest(manifest=type(manifest).__name__), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp).resolve() / "manifest.json"
                manifest.write(target)
                original = target.read_bytes()
                manifest.run_id = "run-2"
                with patch("forward_e2e.execution.file_safety.os.replace", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        manifest.write(target)
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(list(target.parent.iterdir()), [target])
                manifest.write(target)
                self.assertEqual(json.loads(target.read_bytes()), manifest.to_dict())

    def test_missing_submodule_bundles_are_only_skipped_when_explicitly_allowed(self):
        for missing, allow_missing in ((False, False), (True, False), (True, True)):
            with self.subTest(missing=missing, allow_missing=allow_missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                package = root / "package"
                bundles = package / "bundles"
                bundles.mkdir(parents=True)
                lock = _make_lock()
                for role in ("gonka", "contracts"):
                    (bundles / f"{role}.bundle").write_bytes(f"{role}-bundle".encode())
                sub_data = b"synthetic-submodule-bundle"
                sub = SubmoduleRecord("vendor/library", "https://example.org/library.git",
                                      "a" * 40, "bundles/library.bundle",
                                      hashlib.sha256(sub_data).hexdigest())
                lock.gonka["submodules"] = [sub.to_dict()]
                sub_path = package / sub.bundle_relpath
                sub_path.write_bytes(sub_data)
                if missing:
                    sub_path.unlink()
                if missing and not allow_missing:
                    with self.assertRaisesRegex(LockIntegrityError, "missing a submodule bundle"):
                        preserve_source_bundles(lock, package, root / "run")
                else:
                    paths = preserve_source_bundles(lock, package, root / "run",
                                                    allow_missing=allow_missing)
                    self.assertEqual(len(paths), 2 if missing else 3)
                    for path in paths:
                        self.assertEqual(path.read_bytes(),
                                         (package / path.relative_to(root / "run")).read_bytes())


if __name__ == "__main__":
    unittest.main()
