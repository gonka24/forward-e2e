"""Unit tests for forward_e2e.execution.runner_image: locator resolution, immutable image
identity and refusing to execute a lock with a different runner image.

All fixtures are synthetic. No network, Docker, or live chain calls.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from forward_e2e.execution.errors import RunnerImageError
from forward_e2e.execution.runner_image import (
    DEFAULT_RUNNER_IMAGE_LOCATOR,
    RUNNER_IMAGE_DIGEST_ENV,
    RUNNER_IMAGE_ENV,
    RUNNER_IMAGE_ID_ENV,
    RunnerImageIdentity,
    assert_runner_image_matches,
    locator_is_immutable,
    resolve_runner_image,
)

IMAGE_ID = "sha256:" + "a" * 64
OTHER_IMAGE_ID = "sha256:" + "b" * 64
REPO_DIGEST = "ghcr.io/example/a8-runner@sha256:" + "c" * 64


def completed(argv, *, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeDockerRunner:
    """Stands in for ``docker image inspect``."""

    def __init__(self, *, entries=None, returncode=0, stdout=None, stderr=""):
        self.entries = entries
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.stdout is not None:
            body = self.stdout
        else:
            body = json.dumps(self.entries if self.entries is not None else [])
        return completed(argv, returncode=self.returncode, stdout=body, stderr=self.stderr)


def one_entry(image_id=IMAGE_ID, repo_digests=()):
    return [{"Id": image_id, "RepoDigests": list(repo_digests)}]


class ExplodingDockerRunner:
    def __call__(self, argv):  # pragma: no cover - must never be reached
        raise AssertionError(f"docker must not be consulted, got {list(argv)!r}")


class RunnerImageResolutionTests(unittest.TestCase):
    def setUp(self):
        self.emitted = []

    def test_default_locator_is_used_when_nothing_is_supplied(self):
        docker = FakeDockerRunner(entries=one_entry())

        identity = resolve_runner_image(
            docker_runner=docker, env={}, emit=self.emitted.append
        )

        self.assertEqual(DEFAULT_RUNNER_IMAGE_LOCATOR, "forward-e2e-runner:local")
        self.assertEqual(identity.locator, "forward-e2e-runner:local")
        self.assertEqual(docker.calls, [["docker", "image", "inspect", "forward-e2e-runner:local"]])
        self.assertEqual(identity.image_id, IMAGE_ID)
        self.assertEqual(identity.resolved_by, "docker-inspect")
        self.assertFalse(identity.locator_is_immutable)
        self.assertEqual(identity.portability, "local-image-only")

    def test_environment_variable_overrides_the_default_locator(self):
        docker = FakeDockerRunner(entries=one_entry())

        identity = resolve_runner_image(
            docker_runner=docker,
            env={RUNNER_IMAGE_ENV: "ghcr.io/example/a8-runner:ci"},
            emit=self.emitted.append,
        )

        self.assertEqual(RUNNER_IMAGE_ENV, "E2E_RUNNER_IMAGE")
        self.assertEqual(identity.locator, "ghcr.io/example/a8-runner:ci")
        self.assertEqual(
            docker.calls[0], ["docker", "image", "inspect", "ghcr.io/example/a8-runner:ci"]
        )

    def test_explicit_locator_wins_over_the_environment(self):
        docker = FakeDockerRunner(entries=one_entry())

        identity = resolve_runner_image(
            "a8-runner:review",
            docker_runner=docker,
            env={RUNNER_IMAGE_ENV: "a8-runner:ci"},
        )

        self.assertEqual(identity.locator, "a8-runner:review")

    def test_launcher_injected_identity_is_authoritative_and_skips_docker(self):
        identity = resolve_runner_image(
            docker_runner=ExplodingDockerRunner(),
            env={
                RUNNER_IMAGE_ENV: "a8-runner:ci",
                RUNNER_IMAGE_ID_ENV: IMAGE_ID,
                RUNNER_IMAGE_DIGEST_ENV: REPO_DIGEST,
            },
            emit=self.emitted.append,
        )

        self.assertEqual(RUNNER_IMAGE_ID_ENV, "E2E_RUNNER_IMAGE_ID")
        self.assertEqual(RUNNER_IMAGE_DIGEST_ENV, "E2E_RUNNER_IMAGE_DIGEST")
        self.assertEqual(identity.image_id, IMAGE_ID)
        self.assertEqual(identity.repo_digest, REPO_DIGEST)
        self.assertEqual(identity.resolved_by, "launcher-injected")
        self.assertEqual(identity.portability, "registry-hint")

    def test_injected_id_without_digest_is_marked_local_image_only(self):
        identity = resolve_runner_image(
            docker_runner=ExplodingDockerRunner(),
            env={RUNNER_IMAGE_ID_ENV: IMAGE_ID},
            emit=self.emitted.append,
        )

        self.assertEqual(identity.portability, "local-image-only")
        self.assertIsNone(identity.repo_digest)
        self.assertTrue(
            any("docker save / docker load" in message for message in self.emitted)
        )

    def test_registry_digest_is_recorded_when_the_image_has_one(self):
        docker = FakeDockerRunner(entries=one_entry(repo_digests=[REPO_DIGEST]))

        identity = resolve_runner_image(
            "ghcr.io/example/a8-runner:ci",
            docker_runner=docker,
            env={},
            emit=self.emitted.append,
        )

        self.assertEqual(identity.repo_digest, REPO_DIGEST)
        self.assertEqual(identity.portability, "registry-hint")

    def test_a_local_build_digest_is_a_retrieval_hint_not_proof_of_publication(self):
        local_digest = "a8-runner@sha256:" + "d" * 64
        identity = resolve_runner_image(
            "forward-e2e-runner:local", env={},
            docker_runner=FakeDockerRunner(entries=one_entry(repo_digests=[local_digest])),
        )
        self.assertEqual(identity.repo_digest, local_digest)
        self.assertEqual(identity.portability, "registry-hint")

    def test_mutable_tag_resolution_is_announced_with_the_real_image_id(self):
        docker = FakeDockerRunner(entries=one_entry())

        resolve_runner_image(
            "forward-e2e-runner:local", docker_runner=docker, env={}, emit=self.emitted.append
        )

        self.assertTrue(any("is a mutable tag" in message for message in self.emitted))
        self.assertTrue(any(IMAGE_ID in message for message in self.emitted))

    def test_absent_null_or_empty_repo_digests_preserve_local_image_only_resolution(self):
        for representation in ("absent", "null", "empty"):
            with self.subTest(representation=representation):
                entries = one_entry()
                if representation == "absent":
                    del entries[0]["RepoDigests"]
                elif representation == "null":
                    entries[0]["RepoDigests"] = None
                identity = resolve_runner_image(
                    env={}, docker_runner=FakeDockerRunner(entries=entries)
                )
                self.assertEqual(identity.image_id, IMAGE_ID)
                self.assertIsNone(identity.repo_digest)
                self.assertEqual(identity.portability, "local-image-only")

    def test_repo_digests_of_the_wrong_container_type_are_rejected_without_coercion(self):
        for digests in (REPO_DIGEST, "", 123, 0, False, {}, {"digest": REPO_DIGEST}):
            with self.subTest(digests=digests):
                entries = one_entry(repo_digests=[REPO_DIGEST])
                entries[0]["RepoDigests"] = digests
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(env={}, docker_runner=FakeDockerRunner(entries=entries))
                self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
                self.assertEqual(cm.exception.exit_code, 1)
                self.assertIn("list of nonempty strings", str(cm.exception))

    def test_invalid_repo_digest_elements_are_rejected_even_after_a_valid_digest(self):
        for digest in (None, 123, 0, False, {}, [], "", "   "):
            with self.subTest(digest=digest):
                entries = one_entry(repo_digests=[REPO_DIGEST, REPO_DIGEST])
                entries[0]["RepoDigests"][1] = digest
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(env={}, docker_runner=FakeDockerRunner(entries=entries))
                self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
                self.assertEqual(cm.exception.exit_code, 1)
                self.assertIn("list of nonempty strings", str(cm.exception))

    def test_digest_pinned_locator_is_reported_as_immutable(self):
        docker = FakeDockerRunner(entries=one_entry(repo_digests=[REPO_DIGEST]))

        identity = resolve_runner_image(
            REPO_DIGEST, docker_runner=docker, env={}, emit=self.emitted.append
        )

        self.assertTrue(identity.locator_is_immutable)
        self.assertTrue(locator_is_immutable(REPO_DIGEST))
        self.assertTrue(locator_is_immutable("sha256:" + "d" * 64))
        self.assertFalse(locator_is_immutable("forward-e2e-runner:local"))
        self.assertFalse(any("is a mutable tag" in message for message in self.emitted))

    def test_missing_image_produces_an_actionable_error_not_a_traceback(self):
        docker = FakeDockerRunner(returncode=1, stdout="", stderr="Error: No such image")

        with self.assertRaises(RunnerImageError) as cm:
            resolve_runner_image(docker_runner=docker, env={}, emit=self.emitted.append)

        message = str(cm.exception)
        self.assertIn("The runner image is not available locally", message)
        self.assertIn("ops/e2e/build-runner.sh", message)
        self.assertIn("--runner-image", message)
        self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
        self.assertEqual(cm.exception.exit_code, 1)
        self.assertEqual(cm.exception.details["locator"], "forward-e2e-runner:local")
        self.assertIn("No such image", cm.exception.details["stderr"])

    def test_failure_to_launch_docker_is_reported_as_a_structured_runner_error(self):
        for error in (FileNotFoundError(2, "missing"), PermissionError(13, "denied")):
            with self.subTest(error=type(error).__name__), patch(
                "forward_e2e.execution.runner_image.subprocess.run", side_effect=error
            ):
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(env={})
                self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
                self.assertEqual(cm.exception.exit_code, 1)
                self.assertEqual(cm.exception.details["errno"], error.errno)
                self.assertIn("installed and executable on PATH", str(cm.exception))

    def test_an_inspect_timeout_is_reported_with_its_limit_and_retry_guidance(self):
        error = subprocess.TimeoutExpired(["docker", "image", "inspect"], 120.0)
        with patch("forward_e2e.execution.runner_image.subprocess.run", side_effect=error):
            with self.assertRaises(RunnerImageError) as cm:
                resolve_runner_image(env={})
        self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
        self.assertEqual(cm.exception.exit_code, 1)
        self.assertEqual(cm.exception.details["timeout_seconds"], 120.0)
        self.assertIn("Check the Docker daemon and retry", str(cm.exception))

    def test_malformed_digest_values_are_never_classified_as_immutable(self):
        for digest in (IMAGE_ID[:-1], IMAGE_ID + "a", IMAGE_ID[:-1] + "g", "garbage"):
            for locator in (digest, "ghcr.io/example/a8-runner@" + digest):
                with self.subTest(locator=locator):
                    self.assertFalse(locator_is_immutable(locator))
        self.assertFalse(locator_is_immutable("@" + IMAGE_ID))

    def test_a_malformed_launcher_id_is_refused_without_consulting_docker(self):
        for image_id in (IMAGE_ID[:-1], IMAGE_ID + "a", IMAGE_ID[:-1] + "g", "garbage"):
            with self.subTest(image_id=image_id):
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(
                        env={RUNNER_IMAGE_ID_ENV: image_id},
                        docker_runner=ExplodingDockerRunner(),
                    )
                self.assertEqual(cm.exception.details["variable"], RUNNER_IMAGE_ID_ENV)

    def test_inspect_output_that_is_not_json_is_reported_clearly(self):
        docker = FakeDockerRunner(stdout="Cannot connect to the Docker daemon")

        with self.assertRaises(RunnerImageError) as cm:
            resolve_runner_image(docker_runner=docker, env={})

        self.assertIn("docker image inspect did not return JSON", str(cm.exception))
        self.assertEqual(cm.exception.details["locator"], "forward-e2e-runner:local")

    def test_inspect_returning_no_entries_is_reported(self):
        docker = FakeDockerRunner(entries=[])

        with self.assertRaises(RunnerImageError) as cm:
            resolve_runner_image(docker_runner=docker, env={})

        self.assertIn("Runner image inspect returned no entries", str(cm.exception))

    def test_ambiguous_locator_matching_several_images_is_refused(self):
        docker = FakeDockerRunner(
            entries=[{"Id": IMAGE_ID, "RepoDigests": []}, {"Id": OTHER_IMAGE_ID, "RepoDigests": []}]
        )

        with self.assertRaises(RunnerImageError) as cm:
            resolve_runner_image("a8-runner", docker_runner=docker, env={})

        self.assertIn(
            "The runner image locator is ambiguous and matches more than one image",
            str(cm.exception),
        )
        self.assertEqual(cm.exception.details["matches"], 2)

    def test_image_without_an_id_is_refused(self):
        docker = FakeDockerRunner(entries=[{"Id": "", "RepoDigests": []}])

        with self.assertRaises(RunnerImageError) as cm:
            resolve_runner_image(docker_runner=docker, env={})

        self.assertIn("Runner image has no Id", str(cm.exception))

    def test_blank_locator_is_refused_before_talking_to_docker(self):
        for locator in ("", "   "):
            for env in ({}, {RUNNER_IMAGE_ENV: "a8-runner:ci"}):
                with self.subTest(locator=locator, env=env):
                    with self.assertRaises(RunnerImageError) as cm:
                        resolve_runner_image(
                            locator, docker_runner=ExplodingDockerRunner(), env=env
                        )
                    self.assertIn("Runner image locator is empty", str(cm.exception))

    def test_an_inspect_entry_that_is_not_an_object_produces_a_structured_error(self):
        for entry in (None, [], "invalid", 123):
            with self.subTest(entry=entry):
                entries = one_entry()
                entries[0] = entry
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(env={}, docker_runner=FakeDockerRunner(entries=entries))
                self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
                self.assertIn("must be a JSON object", str(cm.exception))

    def test_a_malformed_inspected_id_is_refused_without_coercing_its_type(self):
        for image_id in (IMAGE_ID[:-1], IMAGE_ID + "a", IMAGE_ID[:-1] + "g", "garbage", 123, []):
            with self.subTest(image_id=image_id):
                entries = one_entry()
                entries[0]["Id"] = image_id
                with self.assertRaises(RunnerImageError) as cm:
                    resolve_runner_image(env={}, docker_runner=FakeDockerRunner(entries=entries))
                self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
                self.assertIn("64 lowercase hex digits", str(cm.exception))


class RunnerImageLockEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.locked = {
            "locator": "forward-e2e-runner:local",
            "image_id": IMAGE_ID,
            "repo_digest": None,
            "locator_is_immutable": False,
            "portability": "local-image-only",
            "resolved_by": "docker-inspect",
        }

    def observed(self, image_id=IMAGE_ID, repo_digest=None):
        return RunnerImageIdentity(
            locator="forward-e2e-runner:local",
            image_id=image_id,
            repo_digest=repo_digest,
            locator_is_immutable=False,
            portability="registry-hint" if repo_digest else "local-image-only",
            resolved_by="docker-inspect",
        )

    def test_same_image_id_passes(self):
        self.assertIsNone(assert_runner_image_matches(self.locked, self.observed()))

    def test_different_image_id_never_silently_executes_this_lock(self):
        with self.assertRaises(RunnerImageError) as cm:
            assert_runner_image_matches(self.locked, self.observed(image_id=OTHER_IMAGE_ID))

        message = str(cm.exception)
        self.assertIn("The available runner image is not the one recorded in the lock", message)
        self.assertIn(IMAGE_ID, message)
        self.assertIn(OTHER_IMAGE_ID, message)
        self.assertEqual(cm.exception.code, "RUNNER_IMAGE_MISMATCH")
        self.assertEqual(cm.exception.details["locked_image_id"], IMAGE_ID)
        self.assertEqual(cm.exception.details["observed_image_id"], OTHER_IMAGE_ID)

    def test_locally_pinned_lock_explains_that_a_rebuild_does_not_guarantee_the_locked_id(self):
        with self.assertRaises(RunnerImageError) as cm:
            assert_runner_image_matches(self.locked, self.observed(image_id=OTHER_IMAGE_ID))

        message = str(cm.exception)
        self.assertIn("Transfer it from the host that created the plan", message)
        self.assertIn(
            "Rebuilding from the same tag does not guarantee the locked image id", message
        )

    def test_registry_pinned_lock_tells_the_user_which_digest_to_pull(self):
        locked = dict(self.locked, repo_digest=REPO_DIGEST)

        with self.assertRaises(RunnerImageError) as cm:
            assert_runner_image_matches(locked, self.observed(image_id=OTHER_IMAGE_ID))

        self.assertIn(f"If {REPO_DIGEST} is published, pull it and retry", str(cm.exception))
        self.assertEqual(cm.exception.details["locked_repo_digest"], REPO_DIGEST)

    def test_matching_image_id_is_accepted_with_a_different_registry_manifest_or_mirror(self):
        locked = dict(self.locked, repo_digest=REPO_DIGEST)
        for digest in (
            "ghcr.io/example/a8-runner@sha256:" + "e" * 64,
            REPO_DIGEST.replace("ghcr.io", "mirror.example"),
            None,
        ):
            with self.subTest(digest=digest):
                assert_runner_image_matches(locked, self.observed(repo_digest=digest))

    def test_reordering_inspect_repo_digests_does_not_change_execution_identity(self):
        digests = [REPO_DIGEST, REPO_DIGEST.replace("ghcr.io", "mirror.example")]
        planned = resolve_runner_image(
            env={}, docker_runner=FakeDockerRunner(entries=one_entry(repo_digests=digests))
        )
        observed = resolve_runner_image(
            env={},
            docker_runner=FakeDockerRunner(entries=one_entry(repo_digests=digests[::-1])),
        )
        assert_runner_image_matches(planned.to_dict(), observed)

    def test_matching_registry_digest_never_compensates_for_a_different_image_id(self):
        locked = dict(self.locked, repo_digest=REPO_DIGEST)
        with self.assertRaises(RunnerImageError):
            assert_runner_image_matches(
                locked, self.observed(image_id=OTHER_IMAGE_ID, repo_digest=REPO_DIGEST)
            )

    def test_lock_without_a_recorded_image_id_is_refused(self):
        with self.assertRaises(RunnerImageError) as cm:
            assert_runner_image_matches({"locator": "forward-e2e-runner:local"}, self.observed())

        self.assertIn("The lock does not record a runner image id", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
