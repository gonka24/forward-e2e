"""Regression proofs for the failures found during local E2E review.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""
import copy
import dataclasses
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from forward_e2e.suite.catalog import get_task_by_id
from forward_e2e.execution.builder import Builder, verify_running_images
from forward_e2e.execution.compat import BuildRecipe
from forward_e2e.execution.runlock import BuildManifest
from forward_e2e.execution.runpackage import LoadedRunPackage
from forward_e2e.suite.models import ExecutionStatus, EvidenceStatus
from tests.unit.runner.support.fakes import make_test_lock, new_manifest
from tests.unit.runner.support.fixture_artifacts import task_artifacts
from forward_e2e.suite.verifier import evaluate_task_evidence


class BoundaryCheckpointTests(unittest.TestCase):
    def test_each_boundary_proves_its_catalog_checkpoints_and_rejects_an_unknown_one(self):
        for name in ('go-query-error-classification', 'wasm-abi-boundary', 'contract-network-unconfirmed-policy', 'contract-claim-expiry-policy', 'contract-query-fault-policy'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                task = get_task_by_id(name)
                for relative, data in task_artifacts(task, 'a' * 40).items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                result = evaluate_task_evidence(task, root, root, ExecutionStatus.PASSED, 0)
                self.assertEqual(result[:2], (ExecutionStatus.PASSED, EvidenceStatus.COMPLETE), result)
                self.assertTrue(set(task.expected_checkpoints) <= set(result[2]))
                task = dataclasses.replace(task, expected_checkpoints=[*task.expected_checkpoints, 'unproved'])
                result = evaluate_task_evidence(task, root, root, ExecutionStatus.PASSED, 0)
                self.assertEqual(result[0], ExecutionStatus.FAILED)

    def test_unrelated_or_ignored_cargo_case_cannot_prove_claim_expiry(self):
        task = get_task_by_id('contract-claim-expiry-policy')
        log_name = f'{task.task_id}.log'
        positive = task_artifacts(task, 'a' * 40)[log_name].decode()
        for changed in (positive.replace('claim_expiry_checks_pristine_locked_accounting_and_epoch_overflow', 'unrelated'),
                        positive.replace('... ok', '... ignored')):
            with self.subTest(log=changed), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / log_name).write_text(changed)
                result = evaluate_task_evidence(task, root, root, ExecutionStatus.PASSED, 0)
                self.assertEqual(result[0], ExecutionStatus.FAILED)


class RuntimeDependencyTests(unittest.TestCase):
    def test_pinned_pull_tags_and_inspects_the_compose_reference_and_offline_checks_facts(self):
        reference = 'postgres:18.1-bookworm'
        digest = 'sha256:' + 'a' * 64
        pinned = reference + '@' + digest
        recipe = BuildRecipe('test', (), {}, {}, (), {'postgres': pinned})
        for platform, image_id in [('linux/amd64', 'sha256:' + 'b' * 64),
                                   ('linux/arm64', 'sha256:' + 'c' * 64)]:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as folder:
                calls = []
                expected_calls = [
                    ['docker', 'pull', '--platform', platform, pinned],
                    ['docker', 'tag', pinned, reference],
                    ['docker', 'image', 'inspect', reference],
                ]

                def runner(argv, **kwargs):
                    calls.append(list(argv))
                    # Inspect must prove the Compose tag, not merely the pulled pin.
                    self.assertLessEqual(len(calls), len(expected_calls))
                    self.assertEqual(list(argv), expected_calls[len(calls) - 1])
                    if list(argv) != expected_calls[-1]:
                        return subprocess.CompletedProcess(argv, 0, '', '')
                    data = [{'Id': image_id, 'RepoDigests': ['postgres@' + digest]}]
                    return subprocess.CompletedProcess(argv, 0, json.dumps(data), '')

                manifest = new_manifest()
                Builder(runner=runner).prepare_runtime_dependencies(
                    recipe, manifest=manifest, context={'platform': platform}, roles={'gonka': Path(folder)})
                self.assertEqual(calls, expected_calls)
                manifest = BuildManifest.from_dict(manifest.to_dict())
                observed = verify_running_images(
                    expected_images=[], running={'db': {'image': image_id, 'image_reference': reference}},
                    runtime_external_images=recipe.runtime_external_images,
                    runtime_dependencies=manifest.runtime_dependencies, platform=platform)
                manifest.observed_runtime = {'containers': [{'findings': observed}]}
                lock = make_test_lock(build={'gonka_recipe': recipe.to_dict()}, platform={'docker_platform': platform})
                package = LoadedRunPackage(Path(folder), lock=lock, build_manifest=manifest)
                self.assertEqual(package.provenance_findings(), [])
                for field, value in [('image_id', 'sha256:' + 'd' * 64),
                                     ('image_reference', 'unapproved/image:tag')]:
                    broken = copy.deepcopy(manifest)
                    broken.observed_runtime['containers'][0]['findings'][0][field] = value
                    package.build_manifest = broken
                    self.assertIn('RUNNING_IMAGE_MISMATCH', [f.code for f in package.provenance_findings()])
                broken = copy.deepcopy(manifest)
                broken.runtime_dependencies[0]['platform'] = 'wrong/platform'
                package.build_manifest = broken
                self.assertIn('RUNNING_IMAGE_MISMATCH', [f.code for f in package.provenance_findings()])

    def test_a_failed_digest_pull_never_tags_a_cached_image(self):
        calls = []
        pinned = 'postgres:18@sha256:' + 'a' * 64
        expected_pull = ['docker', 'pull', '--platform', 'linux/amd64', pinned]

        def runner(argv, **kwargs):
            calls.append(list(argv))
            self.assertEqual(list(argv), expected_pull)
            return subprocess.CompletedProcess(argv, 1, '', 'pull failed')

        from forward_e2e.execution.errors import BuildProvenanceError
        recipe = BuildRecipe('test', (), {}, {}, (), {'db': pinned})
        with self.assertRaises(BuildProvenanceError):
            Builder(runner=runner).prepare_runtime_dependencies(
                recipe, manifest=new_manifest(), context={'platform': 'linux/amd64'}, roles={'gonka': Path('.')})
        self.assertEqual(calls, [expected_pull])


if __name__ == '__main__':
    unittest.main()
