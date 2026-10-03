"""Runner image identity.

The runner image carries the harness, the catalog, the verifier and the
toolchain. Its identity must be pinned independently from the code under
test, otherwise choosing a different contracts SHA would silently change the
thing doing the judging.

Rules implemented here:

* A tag is only a *locator*. The identity used for execution is the immutable
  Docker image id.
* A ``RepoDigest`` is recorded when one exists, as a retrieval hint. Docker may
  report one for a locally built image that was never published, so it is not
  proof that replay can pull the image on another host. The image id remains
  the execution identity in either case.
* When the supplied locator is a mutable tag, the resolved image id is printed
  before the lock is written so the user can see what will actually run.
* Before the inner network starts, the observed image id is compared with the
  lock. A different id never executes that lock implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Dict, Mapping, Optional, Sequence

from .gitio import redact

from .errors import RunnerImageError

DEFAULT_RUNNER_IMAGE_LOCATOR = "a8-runner:local"

#: Environment variable used by the host wrappers and by compose to select the
#: runner image. ``--runner-image`` on the CLI overrides it.
RUNNER_IMAGE_ENV = "E2E_RUNNER_IMAGE"

#: Injected by compose into the container so the runner can record the id of
#: the image it is *currently executing inside* without talking to the host
#: daemon (which is never mounted).
RUNNER_IMAGE_ID_ENV = "E2E_RUNNER_IMAGE_ID"
RUNNER_IMAGE_DIGEST_ENV = "E2E_RUNNER_IMAGE_DIGEST"

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def _default_docker_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=120.0, check=False
    )


@dataclass(frozen=True)
class RunnerImageIdentity:
    """Immutable identity of the image that performs the verification."""

    locator: str
    image_id: str
    repo_digest: Optional[str]
    locator_is_immutable: bool
    portability: str
    resolved_by: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "locator": self.locator,
            "image_id": self.image_id,
            "repo_digest": self.repo_digest,
            "locator_is_immutable": self.locator_is_immutable,
            "portability": self.portability,
            "resolved_by": self.resolved_by,
        }


def locator_is_immutable(locator: str) -> bool:
    """A digest reference or a bare image id cannot drift; a tag can."""
    text = str(locator or "")
    if "@" in text:
        repository, text = text.split("@", 1)
        if not repository or any(char.isspace() for char in repository):
            return False
    return _IMAGE_ID.fullmatch(text) is not None


def resolve_runner_image(
    locator: Optional[str] = None,
    *,
    docker_runner=None,
    env: Optional[Mapping[str, str]] = None,
    emit=None,
) -> RunnerImageIdentity:
    """Resolve the locator into an immutable identity using ``docker inspect``.

    ``docker_runner`` exists so tests can supply a fake process runner; there
    is no Docker interaction at import time.
    """
    environ = dict(env if env is not None else os.environ)
    # An explicit empty locator is an input error, not a request for a fallback.
    chosen = (
        locator if locator is not None
        else environ.get(RUNNER_IMAGE_ENV) or DEFAULT_RUNNER_IMAGE_LOCATOR
    ).strip()
    if not chosen:
        raise RunnerImageError("Runner image locator is empty")

    # Inside the container we may already know our own identity because the
    # launcher injected it. That is authoritative and avoids needing a daemon.
    injected_id = (environ.get(RUNNER_IMAGE_ID_ENV) or "").strip()
    injected_digest = (environ.get(RUNNER_IMAGE_DIGEST_ENV) or "").strip() or None
    if injected_id:
        if not _IMAGE_ID.fullmatch(injected_id):
            raise RunnerImageError(
                "Launcher-injected runner image id must be 'sha256:' followed by 64 lowercase hex digits",
                {"variable": RUNNER_IMAGE_ID_ENV},
            )
        immutable = locator_is_immutable(chosen)
        identity = RunnerImageIdentity(
            locator=chosen,
            image_id=injected_id,
            repo_digest=injected_digest,
            locator_is_immutable=immutable,
            portability="registry-hint" if injected_digest else "local-image-only",
            resolved_by="launcher-injected",
        )
        _announce(identity, emit)
        return identity

    runner = docker_runner or _default_docker_runner
    try:
        proc = runner(["docker", "image", "inspect", chosen])
    except subprocess.TimeoutExpired as exc:
        raise RunnerImageError(
            "docker image inspect timed out. Check the Docker daemon and retry.",
            {"locator": chosen, "timeout_seconds": exc.timeout},
        ) from exc
    except OSError as exc:
        raise RunnerImageError(
            "Could not start docker image inspect. Check that Docker is installed "
            "and executable on PATH.",
            {"locator": chosen, "errno": exc.errno},
        ) from exc
    if getattr(proc, "returncode", 1) != 0:
        raise RunnerImageError(
            "The runner image is not available locally. Build it once with "
            "ops/e2e/build-runner.sh (or Build-Runner.ps1), or pass --runner-image "
            "with an image that is present.",
            {"locator": chosen, "stderr": redact(getattr(proc, "stderr", "") or "")[-1000:]},
        )
    try:
        parsed = json.loads(getattr(proc, "stdout", "") or "[]")
    except json.JSONDecodeError as exc:
        raise RunnerImageError(
            "docker image inspect did not return JSON", {"locator": chosen}
        ) from exc
    if not isinstance(parsed, list) or not parsed:
        raise RunnerImageError("Runner image inspect returned no entries", {"locator": chosen})
    if len(parsed) > 1:
        raise RunnerImageError(
            "The runner image locator is ambiguous and matches more than one image",
            {"locator": chosen, "matches": len(parsed)},
        )
    entry = parsed[0]
    if not isinstance(entry, dict):
        raise RunnerImageError(
            "Runner image inspect entry must be a JSON object", {"locator": chosen}
        )
    image_id = entry.get("Id")
    if image_id is None or image_id == "":
        raise RunnerImageError("Runner image has no Id", {"locator": chosen})
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise RunnerImageError(
            "Inspected runner image id must be 'sha256:' followed by 64 lowercase hex digits",
            {"locator": chosen},
        )
    digests = entry.get("RepoDigests")
    if digests is None:
        digests = []
    # Do not coerce malformed metadata into a registry retrieval hint.
    if not isinstance(digests, list) or any(
        not isinstance(digest, str) or not digest.strip() for digest in digests
    ):
        raise RunnerImageError(
            "Runner image RepoDigests must be a list of nonempty strings",
            {"locator": chosen},
        )
    repo_digest = digests[0] if digests else None

    identity = RunnerImageIdentity(
        locator=chosen,
        image_id=image_id,
        repo_digest=repo_digest,
        locator_is_immutable=locator_is_immutable(chosen),
        portability="registry-hint" if repo_digest else "local-image-only",
        resolved_by="docker-inspect",
    )
    _announce(identity, emit)
    return identity


def _announce(identity: RunnerImageIdentity, emit) -> None:
    if emit is None:
        return
    if not identity.locator_is_immutable:
        emit(
            f"Runner image locator {identity.locator!r} is a mutable tag. "
            f"It currently resolves to image id {identity.image_id}."
        )
    if identity.portability == "local-image-only":
        emit(
            "This runner image has no registry digest. Replaying this plan elsewhere "
            "requires that host to have the exact image, for example by transferring it "
            "(docker save / docker load). The runner will not substitute a new build "
            "from a similarly named tag."
        )
    elif identity.portability == "registry-hint":
        emit(
            "A RepoDigest was recorded as a retrieval hint. Docker does not prove that "
            "this image was published to a registry; replay still requires the exact image id."
        )


def assert_runner_image_matches(
    locked: Mapping[str, Any],
    observed: RunnerImageIdentity,
) -> None:
    """Fail before the inner network starts if the image is not the locked one."""
    expected_id = str(locked.get("image_id") or "")
    if not expected_id:
        raise RunnerImageError("The lock does not record a runner image id")
    if observed.image_id != expected_id:
        expected_digest = locked.get("repo_digest")
        hint = (
            f"If {expected_digest} is published, pull it and retry; otherwise transfer "
            "the exact image with docker save / docker load."
            if expected_digest
            else (
                "The locked image has no registry digest, so it cannot be pulled. "
                "Transfer it from the host that created the plan "
                "(docker save | docker load). Rebuilding from the same tag does not "
                "guarantee the locked image id; a different id is refused."
            )
        )
        raise RunnerImageError(
            "The available runner image is not the one recorded in the lock. " + hint,
            {
                "locked_image_id": expected_id,
                "observed_image_id": observed.image_id,
                "locked_repo_digest": expected_digest,
                "observed_repo_digest": observed.repo_digest,
            },
        )
    # RepoDigests are retrieval hints, not execution identity: multiple manifests
    # and repositories can reference the same image, in no canonical order.


__all__ = [
    "DEFAULT_RUNNER_IMAGE_LOCATOR",
    "RUNNER_IMAGE_DIGEST_ENV",
    "RUNNER_IMAGE_ENV",
    "RUNNER_IMAGE_ID_ENV",
    "RunnerImageIdentity",
    "assert_runner_image_matches",
    "locator_is_immutable",
    "resolve_runner_image",
]
