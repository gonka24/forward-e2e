"""Turning a plan into real artefacts, and proving they came from the plan.

This module is the answer to "a new SHA must reach the actual binaries, not
just a new caption". Three separate proofs are produced:

1. **Source snapshot -> build inputs.** The selected commits are built exactly
   as materialised; nothing is applied or committed on top of them (the
   executor proves before and after the build that both snapshots are still
   pristine, see :mod:`ops.a8.source_snapshot`). Every staged build context and
   every declared output must land under the build directory: a step that
   would write into a snapshot is refused before it runs.
2. **Build inputs -> images.** The chain images are built from those inputs
   and their image ids are recorded together with every explicit
   ``--build-arg``. An image that predates the build is only accepted when the
   local provenance ledger proves that this exact image id was produced by an
   earlier build of the *same* component identity (selected SHA, recipe
   fingerprint, adapter, platform, runner image) -- the one case where Docker
   legitimately returns a cached image. Anything else is refused.
3. **Images -> running binary.** The version probe of the live node must report
   the selected commit itself, and the ``wasmd``/``wasmvm`` versions it reports
   must equal the versions measured from the selected sources' own ``go.mod``.

Nothing here is executed at import time, and every external command is
injected through a runner callable so the logic is testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # Only the annotation needs this. Importing it for real would tie the
    # builder to the cancellation module, which the executor is free to leave
    # out entirely.
    from .cancel import Cancellation

from .compat import (
    BuildRecipe,
    BuildStep,
    CompatibilityAdapter,
    ExpectationKind,
)
from .errors import (
    BuildOutputInSourceError,
    BuildProvenanceError,
    ObservedVersionError,
)
from .gitio import redact
from .runlock import BuildManifest, content_sha256, utc_now_iso
from .sources import resolve_within, sha256_path

CommandRunner = Callable[..., subprocess.CompletedProcess]


def _default_runner(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# stage 2: builds
# ---------------------------------------------------------------------------
_ISO_TRAILING_Z = re.compile(r"Z$")


def _parse_docker_time(value: str) -> Optional[float]:
    """Parse Docker's RFC3339 timestamp into a POSIX timestamp."""
    import datetime as dt

    text = (value or "").strip()
    if not text:
        return None
    text = _ISO_TRAILING_Z.sub("+00:00", text)
    # Docker prints nanoseconds; datetime accepts at most microseconds.
    match = re.match(r"(.*\.\d{1,6})\d*([+-]\d{2}:\d{2})$", text)
    if match:
        text = match.group(1) + match.group(2)
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


@dataclass
class ImageObservation:
    reference: str
    image_id: str
    repo_digests: List[str]
    created_epoch: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reference": self.reference,
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
            "created_epoch": self.created_epoch,
        }


# ---------------------------------------------------------------------------
# image provenance ledger
#
# Docker is content-addressable: building the same sources twice legitimately
# returns the *previous* image, keeping its original creation time. A timestamp
# alone therefore cannot distinguish
#
#   (a) a correct rebuild that the daemon served from cache, from
#   (b) a genuinely stale image left over from different sources.
#
# The ledger removes the ambiguity by remembering, for every image id this tool
# ever accepted, which source identity produced it. A cached image is accepted
# only if the ledger says that exact image id came from this exact identity;
# anything unknown is still refused, so the check is strictly stronger than a
# timestamp comparison, never weaker.
#
# It lives in the persistent workspace next to the run lock, i.e. in the same
# volume that survives ``--rm``; losing it only costs a rebuild.
# ---------------------------------------------------------------------------
IMAGE_LEDGER_FILENAME = "image-provenance.json"
IMAGE_LEDGER_SCHEMA = "a8.e2e.image-provenance/v1"


def image_ledger_path(root: Path) -> Path:
    return Path(root) / IMAGE_LEDGER_FILENAME


def source_fingerprint(values: Mapping[str, Any]) -> str:
    """A stable digest of everything that decides what an image contains."""
    import hashlib

    payload = json.dumps(
        {str(key): values[key] for key in sorted(values)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_image_ledger(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Share the definition of a readable ledger with corruption recovery."""
    if path is None:
        return None
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # A missing or damaged ledger proves nothing, so it is treated as empty
        # and the conservative branch (refuse the old image) applies.
        return None
    images = document.get("images") if isinstance(document, dict) else None
    if not isinstance(images, dict):
        return None
    # A syntactically valid document can still contain damaged metadata. These
    # lists are merged into sets when recording a build; reject malformed
    # entries before they can either prove a cache hit or crash that merge.
    # Missing fields remain valid for ledgers written by older producers.
    for entry in images.values():
        if not isinstance(entry, dict):
            return None
        for field in ("source_fingerprints", "run_fingerprints", "produced_by"):
            if field in entry and (
                not isinstance(entry[field], list)
                or any(not isinstance(value, str) for value in entry[field])
            ):
                return None
        if "source_fingerprint" in entry and not isinstance(entry["source_fingerprint"], str):
            return None
    return images


def load_image_ledger(path: Optional[Path]) -> Dict[str, Any]:
    images = _read_image_ledger(path)
    return images if images is not None else {}


def ledger_key(reference: str, image_id: str) -> str:
    return f"{reference}@@{image_id}"


def ledger_is_damaged(path: Optional[Path]) -> bool:
    """True only when the file exists but is not a readable ledger document.

    ``load_image_ledger`` deliberately flattens "absent", "empty" and "corrupt"
    into an empty mapping: for proving a cache hit all three mean the same
    thing. Deciding whether to preserve the original needs the distinction back,
    otherwise a valid ledger that simply has no images yet would be reported as
    damage and preserved as ``.corrupt-<epoch>``.
    """
    if path is None or not Path(path).is_file():
        return False
    return _read_image_ledger(path) is None



def ledger_fingerprints(entry: Optional[Mapping[str, Any]]) -> List[str]:
    """Every source identity recorded for one image id.

    The same image id can legitimately belong to more than one identity (a
    contracts-only change does not alter a chain image), so the record holds a
    list. The singular key is still read for ledgers written before that.
    """
    if not isinstance(entry, Mapping):
        return []
    values = entry.get("source_fingerprints")
    if isinstance(values, (list, tuple)):
        recorded = [str(v) for v in values if v]
    else:
        recorded = []
    single = entry.get("source_fingerprint")
    if single and str(single) not in recorded:
        recorded.append(str(single))
    return recorded


#: How a source role is named in the build context. The context carries paths
#: under the role name and commits under a separate key, and the identity needs
#: the commit.
_SOURCE_CONTEXT_KEYS = {
    # The selected commit itself: nothing is prepared on top of it any more,
    # so the commit that is built is the commit that was selected.
    "gonka": "gonka_sha",
    "contracts": "contracts_sha",
}

#: Roles whose directories are source snapshots. Nothing a build step
#: produces or stages may resolve inside one of them.
PROTECTED_SOURCE_ROLES = ("gonka", "contracts")


def step_source_roles(step: BuildStep, roles: Mapping[str, Any]) -> List[str]:
    """Which source trees a step actually reads.

    A step reads the tree it runs in, plus any tree it names through a
    ``{role}`` placeholder in its argv or its environment. Nothing else: a
    commit that never reaches the command cannot change its output, and
    treating it as an input is what made every chain image depend on the
    contracts commit.
    """
    used = {step.cwd_role}
    text = " ".join(
        [
            *step.argv,
            *[f"{key}={value}" for key, value in dict(step.env).items()],
            *[copy.source for copy in step.stage],
        ]
    )
    for role in roles:
        if "{" + str(role) + "}" in text:
            used.add(str(role))
    # Only source trees carry an identity. The build directory is a role too
    # (steps run in it), but its path changes on every run and says nothing
    # about what the output contains.
    return sorted(role for role in used if role in roles and role in _SOURCE_CONTEXT_KEYS)


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def staged_tree_digest(root: Path) -> Dict[str, Any]:
    """Content digest of a staged build context (paths, modes, bytes, link targets).

    Recorded in the build manifest so a reviewer can see which exact bytes a
    ``docker build`` received, and compare them with the snapshot offline.
    """
    root = Path(root)
    digest = hashlib.sha256()
    files = 0
    if root.is_file():
        return {
            "kind": "file", "files": 1, "sha256": sha256_path(root),
            "mode": format(stat.S_IMODE(root.stat().st_mode), "03o"),
        }
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in dirnames:
            directory = Path(current) / name
            if directory.is_symlink():
                continue  # The link itself is included with filenames below.
            rel = directory.relative_to(root).as_posix()
            mode = format(stat.S_IMODE(directory.stat().st_mode), "03o")
            digest.update(f"directory\0{mode}\0{rel}\n".encode("utf-8", "surrogateescape"))
        for name in sorted(filenames + [d for d in dirnames if (Path(current) / d).is_symlink()]):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                value = "link:" + os.readlink(path)
            else:
                value = sha256_path(path)
            mode = format(stat.S_IMODE(path.lstat().st_mode), "03o")
            digest.update(f"file\0{mode}\0{rel}\0{value}\n".encode("utf-8", "surrogateescape"))
            files += 1
    return {"kind": "tree", "files": files, "sha256": digest.hexdigest()}


class Builder:
    """Executes a build recipe and records what it really produced."""

    def __init__(
        self,
        *,
        runner: Optional[CommandRunner] = None,
        log_dir: Optional[Path] = None,
        emit: Optional[Callable[[str], None]] = None,
        ledger_path: Optional[Path] = None,
        source_fingerprint: Optional[str] = None,
        cancellation: Optional["Cancellation"] = None,
    ):
        self.runner = runner or _default_runner
        self.log_dir = Path(log_dir) if log_dir else None
        self.emit = emit or (lambda message: None)
        #: Optional ``ops.a8.e2e.cancel.Cancellation``. Checked only *after* a
        #: step's log has been written, so a killed build still leaves its
        #: output behind instead of vanishing with the exception.
        self.cancellation = cancellation
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.protected_roles = tuple(PROTECTED_SOURCE_ROLES)
        #: Whole-run fingerprint retained as ledger context. run_recipe computes
        #: the component identity used for cache decisions independently.
        self.source_fingerprint = source_fingerprint

    # -- ledger --------------------------------------------------------
    def _ledger_entry(self, reference: str, image_id: str) -> Optional[Dict[str, Any]]:
        entry = load_image_ledger(self.ledger_path).get(ledger_key(reference, image_id))
        return entry if isinstance(entry, dict) else None

    def _remember_image(
        self,
        observation: ImageObservation,
        *,
        step_id: str,
        recipe_id: str,
        identity: Optional[str] = None,
    ) -> None:
        """Record that this identity produced this image id. Never fatal.

        The identity recorded is the *component* identity, because that is what
        the next build will compare against. The run-level fingerprint is kept
        alongside it as context for a human reading the ledger, never as the
        thing a cache decision is made on.

        Concurrency is handled one level up: the runner holds an exclusive flock
        on the workspace for the whole execution, so two builds cannot interleave
        here.
        """
        recorded_identity = identity or self.source_fingerprint
        if self.ledger_path is None or not recorded_identity:
            return
        images = load_image_ledger(self.ledger_path)
        if ledger_is_damaged(self.ledger_path):
            # Unreadable, not absent and not merely empty. Keep the damaged file
            # for inspection instead of overwriting the only record of previous
            # builds.
            if not self._preserve_damaged_ledger():
                return
        key = ledger_key(observation.reference, observation.image_id)
        previous = images.get(key) if isinstance(images.get(key), dict) else {}
        fingerprints = ledger_fingerprints(previous)
        if recorded_identity not in fingerprints:
            fingerprints.append(recorded_identity)
        images[key] = {
            "reference": observation.reference,
            "image_id": observation.image_id,
            # An image id can belong to several identities; none of them is
            # dropped, because each is a proof that must survive the next build.
            "source_fingerprints": sorted(fingerprints),
            # Context only, never compared: which whole-run identity happened to
            # be building when this image was recorded.
            "run_fingerprints": sorted(
                {
                    *(previous.get("run_fingerprints") or []),
                    *([self.source_fingerprint] if self.source_fingerprint else []),
                }
            ),
            "created_epoch": observation.created_epoch,
            "first_recorded_at_utc": previous.get("first_recorded_at_utc") or utc_now_iso(),
            "last_recorded_at_utc": utc_now_iso(),
            "produced_by": sorted(
                {
                    *(previous.get("produced_by") or []),
                    f"{recipe_id}:{step_id}",
                }
            ),
        }
        document = {"schema": IMAGE_LEDGER_SCHEMA, "images": images}
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ledger_path.with_name(self.ledger_path.name + ".tmp")
            temporary.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(self.ledger_path)
        except OSError as exc:  # pragma: no cover - disk failure
            # The ledger is an optimisation for the *next* build. Failing to
            # write it must not invalidate a build that already succeeded.
            self.emit(f"[build] warning: could not update the image ledger: {exc}")

    def _preserve_damaged_ledger(self) -> bool:
        if self.ledger_path is None:
            return False
        base_name = self.ledger_path.name + f".corrupt-{int(time.time())}"
        attempt = 0
        while True:
            suffix = "" if attempt == 0 else f"-{attempt}"
            backup = self.ledger_path.with_name(base_name + suffix)
            try:
                # Linking is atomic and refuses an existing destination. Keep
                # the original in place until the new ledger replaces it below;
                # atomic replacement leaves this backup's inode untouched.
                os.link(self.ledger_path, backup)
                break
            except FileExistsError:
                attempt += 1
            except OSError as exc:
                self.emit(
                    f"[build] warning: could not preserve the damaged image ledger "
                    f"at {self.ledger_path}; skipping ledger update: {exc}"
                )
                return False
        self.emit(
            f"[build] warning: the image ledger was unreadable and was kept at {backup}; "
            "previously cached images will have to be rebuilt once."
        )
        return True

    # -- helpers -------------------------------------------------------
    def inspect_image(self, reference: str) -> Optional[ImageObservation]:
        """Return None only for an explicit image-not-found response.

        Callers use absence as proof that removal succeeded. Daemon failures or
        malformed replies must stop the build, never establish that proof.
        """
        proc = self.runner(
            ["docker", "image", "inspect", reference], timeout=120.0
        )
        if getattr(proc, "returncode", 1) != 0:
            stderr = (getattr(proc, "stderr", "") or "").strip()
            if re.fullmatch(
                r"(?:Error(?: response from daemon)?: )?No such image"
                + r"(?:: " + re.escape(reference) + r")?",
                stderr,
            ):
                return None
            raise BuildProvenanceError(
                "Could not inspect a Docker image",
                {"reference": reference, "exit_code": getattr(proc, "returncode", None),
                 "stderr": redact(stderr)[-1000:]},
            )
        try:
            entries = json.loads(getattr(proc, "stdout", "") or "[]")
        except json.JSONDecodeError as exc:
            raise BuildProvenanceError(
                "Docker image inspection returned invalid JSON", {"reference": reference}
            ) from exc
        if (not isinstance(entries, list) or len(entries) != 1
                or not isinstance(entries[0], dict)
                or not isinstance(entries[0].get("Id"), str) or not entries[0]["Id"]):
            raise BuildProvenanceError(
                "Docker image inspection returned an invalid image record",
                {"reference": reference},
            )
        entry = entries[0]
        return ImageObservation(
            reference=reference,
            image_id=str(entry.get("Id") or ""),
            repo_digests=[str(d) for d in (entry.get("RepoDigests") or [])],
            created_epoch=_parse_docker_time(str(entry.get("Created") or "")),
        )

    def substitute(self, template: str, context: Mapping[str, str]) -> str:
        result = template
        for key, value in context.items():
            result = result.replace("{" + key + "}", str(value))
        return result

    # -- recipe execution ----------------------------------------------
    def prepare_runtime_dependencies(self, recipe: BuildRecipe, *, manifest: BuildManifest,
                                     context: Mapping[str, str], roles: Mapping[str, Path]) -> None:
        """Pull immutable indexes, then bind the compose tags in the private daemon.

        A tag already cached on this machine is never the source of truth.
        Docker selects the requested platform from the pinned registry index.
        """
        for label, pinned in recipe.runtime_external_images.items():
            reference, digest = split_runtime_pin(pinned)
            for suffix, argv in (
                ("pull", ("docker", "pull", "--platform", context["platform"], pinned)),
                ("tag", ("docker", "tag", pinned, reference)),
            ):
                self._execute_step(
                    BuildStep(step_id=f"runtime-{label}-{suffix}", argv=argv,
                              cwd_role="gonka", timeout_seconds=600,
                              description="Prepare a pinned external runtime service"),
                    manifest=manifest, context=context, roles=roles,
                )
            image = self.inspect_image(reference)
            if image is None or not image.image_id or not any(
                value.endswith("@" + digest) for value in image.repo_digests
            ):
                raise BuildProvenanceError("Runtime dependency did not resolve to its registry pin",
                                           {"reference": reference, "pinned": pinned})
            manifest.runtime_dependencies.append({
                "reference": reference, "pinned": pinned,
                "platform": context["platform"], "image_id": image.image_id,
                "repo_digests": image.repo_digests,
            })

    def run_recipe(
        self,
        recipe: BuildRecipe,
        *,
        manifest: BuildManifest,
        context: Mapping[str, str],
        roles: Mapping[str, Path],
        started_epoch: float,
        adapter: Optional[CompatibilityAdapter] = None,
    ) -> None:
        """Run every step, then verify each declared artefact really appeared.

        A step whose images cannot be proven is not fatal on the first attempt:
        the unprovable image is removed and the step is run once more. If the
        step then recreates the image, the removal and the recreation together
        are the proof -- the image did not exist when the step started and does
        exist now. Only if that fails too is the run refused, because at that
        point nothing about the image can be established.
        """
        for step in recipe.steps:
            identity = self.component_identity(
                step, recipe=recipe, adapter=adapter, context=context, roles=roles
            )
            attempt = 1
            rebuilt: List[str] = []
            while True:
                self._execute_step(
                    step,
                    manifest=manifest,
                    context=context,
                    roles=roles,
                    identity=identity,
                    attempt=attempt,
                )
                unprovable = self._unprovable_images(
                    step, context=context, started_epoch=started_epoch, identity=identity
                )
                if not unprovable or attempt >= 2:
                    break
                if not self._discard_images(unprovable):
                    # The images could not be removed, so a second run would
                    # prove exactly as little as the first one did.
                    break
                rebuilt = [reference for reference, _ in unprovable]
                attempt += 1

            self._record_images(
                step,
                manifest,
                context,
                started_epoch,
                recipe.recipe_id,
                identity=identity,
                rebuilt_references=rebuilt,
            )
            self._record_files(step, manifest, context)
            # Recording the artefacts of one step means several `docker image
            # inspect` calls; a signal arriving in that window must not wait for
            # the next step, and there may not be one.
            self._stop_if_cancelled(step.step_id, manifest, 0)

    def _stop_if_cancelled(self, step_id: str, manifest: BuildManifest, exit_code: Any) -> None:
        """Turn a requested cancellation into evidence, then into an exception.

        Called only where the step's log and command record are already on
        disk, so the operator keeps everything the build produced. A signal is
        an interruption, not a verdict: the partial evidence stays and the run
        ends as CANCELLED.
        """
        if self.cancellation is None or not self.cancellation.requested:
            return
        manifest.record_failure(
            "RUN_CANCELLED",
            "The operator stopped the run while this build step was running",
            {"step_id": step_id, "exit_code": exit_code},
        )
        self.cancellation.raise_if_requested(f"build-step:{step_id}")

    def _execute_step(
        self,
        step: BuildStep,
        *,
        manifest: BuildManifest,
        context: Mapping[str, str],
        roles: Mapping[str, Path],
        identity: Optional[str] = None,
        attempt: int = 1,
    ) -> Dict[str, Any]:
        """Run one build command and record exactly what was run."""
        argv = [self.substitute(token, context) for token in step.argv]
        cwd = roles.get(step.cwd_role)
        if cwd is None:
            raise BuildProvenanceError(
                "Build step refers to an unknown working directory role",
                {"step_id": step.step_id, "cwd_role": step.cwd_role},
            )
        env = {
            key: self.substitute(value, context) for key, value in dict(step.env).items()
        }
        # Refuse before anything runs: a step that would write into a source
        # snapshot must not get the chance to, even once.
        self.assert_outputs_outside_sources(step, manifest=manifest, context=context, roles=roles)
        self.emit(f"[build] {step.step_id}: {' '.join(argv) or '(stage)'}")
        record: Dict[str, Any] = {
            "step_id": step.step_id,
            "argv": argv,
            "cwd": str(cwd),
            "env": env,
            "build_args": {
                name: self.substitute(value, context) for name, value in step.build_args
            },
            "attempt": attempt,
            "component_identity": identity,
            "started_at_utc": utc_now_iso(),
        }
        if step.is_stage:
            record["staged"] = self._stage(step, manifest=manifest, context=context)
            record["exit_code"] = 0
            record["completed_at_utc"] = utc_now_iso()
            manifest.build_commands.append(record)
            manifest.staged_contexts.extend(record["staged"])
            self._stop_if_cancelled(step.step_id, manifest, 0)
            return record
        merged_env = dict(_os_environ())
        merged_env.update(env)
        try:
            proc = self.runner(
                argv, cwd=str(cwd), env=merged_env, timeout=float(step.timeout_seconds)
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired may carry bytes even for a text-mode runner. Keep
            # its partial output before unwinding through the executor's export.
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stdout = redact(stdout)
            stderr = redact(stderr)
            self._write_log(
                step.step_id, subprocess.CompletedProcess(argv, None, stdout, stderr),
                attempt=attempt,
            )
            record["exit_code"] = None
            record["completed_at_utc"] = utc_now_iso()
            manifest.build_commands.append(record)
            self._stop_if_cancelled(step.step_id, manifest, None)
            manifest.record_failure(
                "BUILD_STEP_TIMEOUT",
                f"Build step {step.step_id!r} timed out",
                {"step_id": step.step_id, "attempt": attempt,
                 "timeout_seconds": step.timeout_seconds, "stderr": stderr[-4000:]},
            )
            raise
        record["exit_code"] = getattr(proc, "returncode", None)
        record["completed_at_utc"] = utc_now_iso()
        self._write_log(step.step_id, proc, attempt=attempt)
        manifest.build_commands.append(record)
        self._stop_if_cancelled(step.step_id, manifest, record["exit_code"])
        if record["exit_code"] != 0:
            manifest.record_failure(
                "BUILD_STEP_FAILED",
                f"Build step {step.step_id!r} failed",
                {
                    "step_id": step.step_id,
                    "attempt": attempt,
                    "exit_code": record["exit_code"],
                    "stderr": redact(getattr(proc, "stderr", "") or "")[-4000:],
                },
            )
            raise BuildProvenanceError(
                f"Build step {step.step_id!r} failed; no artefacts are recorded as complete",
                {"step_id": step.step_id, "exit_code": record["exit_code"]},
            )
        return record

    # -- source-snapshot boundary -------------------------------------
    #: Flags whose *next* argument is a directory the tool writes into.
    OUTPUT_DIR_FLAGS = frozenset(
        {"--project-cache-dir", "--target-dir", "--output", "-o", "--out-dir", "--build-dir"}
    )
    #: ``-Pname=value`` / ``-Dname=value`` properties that name output roots.
    OUTPUT_PROPERTY_PREFIXES = (
        "-Pa8.outRoot=",
        "-Pkotlin.project.persistent.dir=",
        "-Dkotlin.project.persistent.dir=",
    )
    #: Environment variables that name directories a tool writes into.
    OUTPUT_ENV_NAMES = frozenset({"GRADLE_USER_HOME", "CARGO_TARGET_DIR", "GOCACHE", "GOMODCACHE"})

    def output_locations(self, step: BuildStep, context: Mapping[str, str]) -> List[Tuple[str, str]]:
        """Every location this step declares it writes, resolved, with a label."""
        build_out = Path(context.get("build_out", "."))
        found: List[Tuple[str, str]] = []
        for copy in step.stage:
            found.append(("stage destination", self.substitute(copy.destination, context)))
        for relative in step.produces_files:
            found.append(("declared output", str(build_out / self.substitute(relative, context))))
        argv = [self.substitute(token, context) for token in step.argv]
        for index, token in enumerate(argv):
            if token in self.OUTPUT_DIR_FLAGS and index + 1 < len(argv):
                found.append((token, argv[index + 1]))
            for prefix in self.OUTPUT_PROPERTY_PREFIXES:
                if token.startswith(prefix):
                    found.append((prefix.rstrip("="), token[len(prefix):]))
        for key, value in dict(step.env).items():
            if key in self.OUTPUT_ENV_NAMES:
                found.append((key, self.substitute(value, context)))
        return found

    def assert_outputs_outside_sources(
        self,
        step: BuildStep,
        *,
        manifest: BuildManifest,
        context: Mapping[str, str],
        roles: Mapping[str, Path],
    ) -> None:
        """Refuse a step whose outputs, caches or staged contexts are inside a snapshot.

        Why: upstream's own build targets write into the checkout
        (``.docker-context``, ``build/``, ``.gradle``). The immutable-source
        model reproduces those calls with every output redirected; this check
        is what keeps a future recipe edit from quietly pointing one of them
        back into the tree. Staged contexts must additionally be under the
        build directory, because they are removed and recreated there.
        """
        protected = {role: Path(roles[role]) for role in self.protected_roles if role in roles}
        build_out = Path(context.get("build_out", "."))
        offenders: List[Dict[str, str]] = []
        for label, location in self.output_locations(step, context):
            if not location:
                continue
            for role, root in protected.items():
                if _is_within(Path(location), root):
                    offenders.append({"what": label, "path": location, "source_role": role})
            if label == "stage destination" and not _is_within(Path(location), build_out):
                offenders.append({"what": label, "path": location, "source_role": "outside build_out"})
        if offenders:
            manifest.record_failure(
                BuildOutputInSourceError.code,
                "A build step would write into a source snapshot",
                {"step_id": step.step_id, "offenders": offenders},
            )
            raise BuildOutputInSourceError(
                "Refusing a build step whose outputs, caches or staged build context resolve "
                "inside a source snapshot. The selected sources must stay byte-for-byte what "
                "the selected commit says.",
                {"step_id": step.step_id, "offenders": offenders},
            )

    def _stage(
        self, step: BuildStep, *, manifest: BuildManifest, context: Mapping[str, str]
    ) -> List[Dict[str, Any]]:
        """Copy each declared input into its staged context and prove the copy."""
        staged: List[Dict[str, Any]] = []
        for copy in step.stage:
            source = Path(self.substitute(copy.source, context))
            destination = Path(self.substitute(copy.destination, context))
            if not source.exists() or source.is_symlink():
                manifest.record_failure(
                    "BUILD_STAGE_SOURCE_MISSING",
                    "A staged build input is missing (or is a symlink, which is never followed)",
                    {"step_id": step.step_id, "source": str(source)},
                )
                raise BuildProvenanceError(
                    "A staged build input is missing",
                    {"step_id": step.step_id, "source": str(source)},
                )
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
            source_digest = staged_tree_digest(source)
            staged_digest = staged_tree_digest(destination)
            if source_digest != staged_digest:
                manifest.record_failure(
                    "BUILD_STAGE_MISMATCH",
                    "A staged build context is not a byte-identical copy of its source",
                    {"step_id": step.step_id, "source": str(source),
                     "source_digest": source_digest, "staged_digest": staged_digest},
                )
                raise BuildProvenanceError(
                    "A staged build context is not a byte-identical copy of its source",
                    {"step_id": step.step_id, "source": str(source)},
                )
            staged.append(
                {
                    "step_id": step.step_id,
                    "what": copy.what,
                    "source": str(source),
                    "destination": str(destination),
                    "digest": staged_digest,
                }
            )
        return staged

    # -- per-component identity ----------------------------------------
    def component_identity(
        self,
        step: BuildStep,
        *,
        recipe: Optional[BuildRecipe] = None,
        adapter: Optional[CompatibilityAdapter] = None,
        context: Mapping[str, str],
        roles: Mapping[str, Path],
    ) -> str:
        """What *this step's* output is allowed to depend on.

        The run-level fingerprint answers "is anything about this run
        different", which is the wrong question for a cache: a contracts commit
        that no chain image ever reads would invalidate every chain image.
        The identity below includes the step's source inputs and conservative
        recipe, adapter and runner boundaries. Different adapters do not share
        an identity merely because their build commands are identical:

        * the content of the step itself (argv, env, cwd role, declared
          outputs) -- a changed command is a changed artefact;
        * the recipe identifier;
        * the adapter's content hash, which already covers the
          whole recipe and the runtime expectations;
        * the target platform and the runner image the build ran in;
        * the commit of each source role the step reads, and only those.
        """
        sources = {
            role: str(context.get(_SOURCE_CONTEXT_KEYS.get(role, role), ""))
            for role in step_source_roles(step, roles)
        }
        return source_fingerprint(
            {
                "step": content_sha256(step.to_dict()),
                "recipe_id": recipe.recipe_id if recipe is not None else "",
                # The whole recipe, not only this step: a changed sibling step
                # (for example a staging rule) can change what this one reads.
                "recipe_fingerprint": recipe.fingerprint() if recipe is not None else "",
                # ``git describe`` output reaches the binary through LDFLAGS,
                # so it is an input even for an unchanged commit.
                "gonka_version": str(context.get("gonka_version", ""))
                if "gonka" in sources else "",
                "adapter_content_hash": adapter.content_hash() if adapter is not None else "",
                "platform": str(context.get("platform", "")),
                "runner_image_id": str(context.get("runner_image_id", "")),
                "sources": sources,
            }
        )

    # -- image provenance ------------------------------------------------
    def _unprovable_images(
        self,
        step: BuildStep,
        *,
        context: Mapping[str, str],
        started_epoch: float,
        identity: Optional[str],
    ) -> List[Tuple[str, ImageObservation]]:
        """Declared images that exist but cannot be tied to these sources."""
        unprovable: List[Tuple[str, ImageObservation]] = []
        for reference in step.produces_images:
            resolved = self.substitute(reference, context)
            observation = self.inspect_image(resolved)
            if observation is None:
                continue
            if not self._is_cache_hit(observation, started_epoch):
                continue
            if self._is_proven(resolved, observation, identity) is not None:
                continue
            unprovable.append((resolved, observation))
        return unprovable

    def _discard_images(
        self, unprovable: Sequence[Tuple[str, ImageObservation]]
    ) -> bool:
        """Remove unprovable images so the next attempt has to create them.

        Returns ``True`` only if every one of them is really gone afterwards.
        An image that survives (another tag points at it, the daemon refuses)
        would make the retry meaningless, and pretending otherwise is exactly
        the kind of silent weakening this check exists to prevent.
        """
        for reference, observation in unprovable:
            self.emit(
                f"[build] {reference}: the existing image {observation.image_id[:19]} cannot "
                "be tied to these sources; removing it and building once more."
            )
            self.runner(
                ["docker", "image", "rm", "-f", reference], timeout=300.0
            )
        for reference, _ in unprovable:
            if self.inspect_image(reference) is not None:
                self.emit(
                    f"[build] {reference}: the image is still present after removal, so a "
                    "rebuild would prove nothing."
                )
                return False
        return True

    @staticmethod
    def _is_cache_hit(observation: ImageObservation, started_epoch: float) -> bool:
        # An image created during this build was certainly produced by it. An
        # older one can still be correct -- Docker returns the identical
        # previous image when the build inputs are unchanged -- but only the
        # ledger can say so. An unknown creation time proves nothing either, so
        # it is treated exactly like an old one.
        return (
            observation.created_epoch is None
            or observation.created_epoch < started_epoch
        )

    def _is_proven(
        self, reference: str, observation: ImageObservation, identity: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """The ledger entry proving this image id came from this identity."""
        if not identity:
            return None
        entry = self._ledger_entry(reference, observation.image_id)
        return entry if identity in ledger_fingerprints(entry) else None

    def _record_images(
        self,
        step: BuildStep,
        manifest: BuildManifest,
        context: Mapping[str, str],
        started_epoch: float,
        recipe_id: str = "",
        *,
        identity: Optional[str] = None,
        rebuilt_references: Sequence[str] = (),
    ) -> None:
        for reference in step.produces_images:
            resolved = self.substitute(reference, context)
            observation = self.inspect_image(resolved)
            if observation is None:
                manifest.record_failure(
                    "BUILD_IMAGE_MISSING",
                    "A declared image was not produced by the build step",
                    {"step_id": step.step_id, "reference": resolved},
                )
                raise BuildProvenanceError(
                    "A declared image is missing after its build step",
                    {"step_id": step.step_id, "reference": resolved},
                )
            cache_hit = self._is_cache_hit(observation, started_epoch)
            proof = self._is_proven(resolved, observation, identity) if cache_hit else None
            rebuilt = resolved in set(rebuilt_references)
            if cache_hit and proof is None and rebuilt:
                # The image was removed before this attempt and exists again
                # afterwards, so this step created it. That is a stronger fact
                # than any timestamp, and it is recorded as such rather than
                # left looking like an unexplained cache hit.
                proof = {
                    "kind": "REBUILT_AFTER_REMOVAL",
                    "reference": resolved,
                    "image_id": observation.image_id,
                    "component_identity": identity,
                }
                self.emit(
                    f"[build] {resolved}: rebuilt after removal; the image this step "
                    "produced is now recorded against these sources."
                )
            elif cache_hit and proof is None:
                reason = self._stale_reason(resolved, observation)
                manifest.record_failure(
                    "STALE_IMAGE_REUSED",
                    "The image predates this build and nothing proves it came from "
                    "the selected sources",
                    {
                        "reference": resolved,
                        "image_id": observation.image_id,
                        "image_created_epoch": observation.created_epoch,
                        "build_started_epoch": started_epoch,
                        "reason": reason,
                        "ledger": str(self.ledger_path) if self.ledger_path else None,
                        "recorded_source_fingerprints": ledger_fingerprints(
                            self._ledger_entry(resolved, observation.image_id)
                        ),
                        "expected_component_identity": identity,
                        "rebuild_attempted": bool(rebuilt_references),
                    },
                )
                raise BuildProvenanceError(
                    "Refusing a stale cached image: it was created before this build "
                    "started, it is not recorded as having been produced from the "
                    "selected sources, and rebuilding it after removal did not "
                    "establish that either.",
                    {
                        "reference": resolved,
                        "image_id": observation.image_id,
                        "reason": reason,
                    },
                )
            elif cache_hit:
                self.emit(
                    f"[build] {resolved}: Docker returned the cached image "
                    f"{observation.image_id[:19]}; the ledger records it as built from "
                    "these exact sources."
                )
            entry = observation.to_dict()
            entry["role"] = resolved
            entry["step_id"] = step.step_id
            entry["cache_hit"] = bool(cache_hit)
            entry["component_identity"] = identity
            if proof is not None:
                entry["cache_proof"] = dict(proof)
            manifest.images.append(entry)
            self._remember_image(
                observation, step_id=step.step_id, recipe_id=recipe_id, identity=identity
            )

    def _stale_reason(
        self, reference: str, observation: ImageObservation
    ) -> str:
        entry = self._ledger_entry(reference, observation.image_id)
        if observation.created_epoch is None:
            # The record, when there is one, is the more specific fact: saying
            # "no record" next to a non-empty ``recorded_source_fingerprints``
            # would contradict the same failure's own details.
            return (
                "creation time unknown and no record of this image id"
                if entry is None
                else "creation time unknown and recorded against different sources"
            )
        if entry is None:
            return "no record of this image id"
        return "recorded against different sources"

    def _record_files(
        self,
        step: BuildStep,
        manifest: BuildManifest,
        context: Mapping[str, str],
    ) -> None:
        build_out = Path(context.get("build_out", "."))
        for relative in step.produces_files:
            # Build outputs are evidence: reject escaped paths and symlinks
            # before following them to read or hash files outside this build.
            resolved = resolve_within(
                build_out, self.substitute(relative, context), what="build output path"
            )
            if not resolved.is_file():
                manifest.record_failure(
                    "BUILD_FILE_MISSING",
                    "A declared build output is missing",
                    {"step_id": step.step_id, "path": str(resolved)},
                )
                raise BuildProvenanceError(
                    "A declared build output is missing",
                    {"step_id": step.step_id, "path": str(resolved)},
                )
            entry = {
                "role": relative,
                "step_id": step.step_id,
                "path": str(resolved),
                "sha256": sha256_path(resolved),
            }
            if resolved.suffix == ".wasm":
                manifest.wasm.append(entry)
            else:
                manifest.binaries.append(entry)

    def _write_log(
        self,
        step_id: str,
        proc: subprocess.CompletedProcess,
        *,
        attempt: int = 1,
    ) -> None:
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # The first attempt keeps its historical name so existing tooling and
        # documentation still find it; a retry is written next to it rather than
        # over it, because why the first attempt was not provable is part of the
        # evidence.
        suffix = "" if attempt <= 1 else f".attempt-{attempt}"
        target = self.log_dir / f"build-{step_id}{suffix}.log"
        body = redact((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or ""))
        target.write_text(body, encoding="utf-8")


def _os_environ() -> Mapping[str, str]:
    return os.environ


# ---------------------------------------------------------------------------
# stage 3: observed runtime
# ---------------------------------------------------------------------------
_VERSION_COMMIT_RE = re.compile(r"^\s*commit:\s*([0-9a-fA-F]{40})\s*$", re.MULTILINE)
_GO_DEP_RE = re.compile(r"(github\.com/[^\s@]+)@(v[^\s]+)")


def parse_version_long(text: str) -> Dict[str, str]:
    """Parse ``inferenced version --long`` output into a flat mapping.

    Mirrors the fields the existing harness already relies on so that the two
    readers cannot drift: ``commit``, ``wasmd``, ``wasmvm``, ``go``,
    ``cosmos_sdk_version``.
    """
    parsed: Dict[str, str] = {}
    commit = _VERSION_COMMIT_RE.search(text or "")
    if commit:
        parsed["gonka_source_sha"] = commit.group(1).lower()
    for module, version in _GO_DEP_RE.findall(text or ""):
        if module == "github.com/CosmWasm/wasmd":
            parsed["wasmd"] = version
        elif module == "github.com/CosmWasm/wasmvm/v2":
            parsed["wasmvm"] = version
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("cosmos_sdk_version", "go", "version", "name", "server_name"):
            parsed.setdefault(key, value.strip())
    return parsed


def observe_runtime(
    adapter: CompatibilityAdapter,
    *,
    runner: Optional[CommandRunner] = None,
    container: Optional[str] = None,
) -> Dict[str, Any]:
    """Probe the live node for its real identity."""
    if adapter.harness is None:
        return {}
    exec_runner = runner or _default_runner
    node = container or adapter.harness.node_container
    argv = ["docker", "exec", node, *adapter.harness.version_probe_argv]
    proc = exec_runner(argv, timeout=180.0)
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    if getattr(proc, "returncode", 1) != 0:
        raise ObservedVersionError(
            "Could not read the version of the running chain binary",
            {"argv": argv, "exit_code": getattr(proc, "returncode", None),
             "stderr": redact(stderr)[-1000:]},
        )
    observed = parse_version_long(stdout + "\n" + stderr)
    observed["_raw"] = redact(stdout + stderr)[-4000:]
    observed["_probe_argv"] = argv
    return observed


def verify_observed_runtime(
    adapter: CompatibilityAdapter,
    observed: Mapping[str, Any],
    *,
    sources_root: Path,
    selected_sha: str,
) -> List[Dict[str, Any]]:
    """Compare the running binary against the adapter's expectations.

    Returns the per-expectation comparison records for the build manifest and
    raises on the first mismatch. A missing expected value is also a failure:
    the runner must never pass an unverifiable component.
    """
    results: List[Dict[str, Any]] = []
    for expectation in adapter.runtime:
        actual = observed.get(expectation.field_name)
        expected = expectation.expected_value(
            sources_root=sources_root, selected_sha=selected_sha
        )
        record = {
            "component": expectation.component,
            "field": expectation.field_name,
            "kind": expectation.kind.value,
            "expected": expected,
            "actual": actual,
            "source_of_truth": (
                "selected commit" if expectation.kind is ExpectationKind.SELECTED_COMMIT
                else f"{expectation.go_mod_relpath}:{expectation.go_module}"
                if expectation.kind is ExpectationKind.SOURCE_GO_MODULE
                else expectation.pattern
            ),
        }
        results.append(record)

        if expectation.kind is ExpectationKind.LITERAL_PATTERN:
            pattern = expectation.pattern or ""
            if not actual or re.search(pattern, str(actual)) is None:
                raise ObservedVersionError(
                    "The running component does not match the expected version pattern",
                    record,
                )
            continue

        if expected is None:
            raise ObservedVersionError(
                "The expected runtime version could not be measured from the selected sources, "
                "so the running component cannot be verified.",
                record,
            )
        if actual is None:
            raise ObservedVersionError(
                "The running component did not report the field required for verification",
                record,
            )
        if str(actual).lower() != str(expected).lower():
            extra = {}
            if expectation.kind is ExpectationKind.SELECTED_COMMIT:
                extra["hint"] = (
                    "The binary does not report the selected commit. A previously cached or "
                    "externally supplied image is running instead of the one built from the "
                    "selected sources."
                )
            raise ObservedVersionError(
                "Observed runtime version does not match the selected sources",
                {**record, **extra},
            )
    return results


def split_runtime_pin(pinned: str) -> Tuple[str, str]:
    reference, separator, digest = str(pinned).partition("@sha256:")
    if not separator or not reference or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise BuildProvenanceError("Runtime dependency requires a registry SHA256 pin", {"value": pinned})
    return reference, "sha256:" + digest


def verify_running_images(
    *,
    expected_images: Sequence[Mapping[str, Any]],
    running: Mapping[str, Any],
    runtime_external_images: Optional[Mapping[str, str]] = None,
    runtime_dependencies: Sequence[Mapping[str, Any]] = (),
    platform: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Prove the live containers run the images this build produced.

    ``running`` maps a container name to its Docker image ID, or to the
    ownership record containing both ``image`` and ``image_reference``.
    A service not made from the selected sources is accepted only when it
    matches an explicit registry pin and its recorded platform image ID.
    Merely matching an upstream tag is never enough.
    """
    built_ids = {str(entry.get("image_id")) for entry in expected_images if entry.get("image_id")}
    approved_external: Dict[str, str] = {}
    for label, pinned in (runtime_external_images or {}).items():
        reference, digest = split_runtime_pin(pinned)
        records = [r for r in runtime_dependencies if r.get("reference") == reference]
        if len(records) != 1:
            raise BuildProvenanceError("Missing or duplicate runtime dependency record", {"label": label})
        record = records[0]
        if (record.get("pinned") != pinned or not platform or record.get("platform") != platform
                or not record.get("image_id") or not any(
                    str(value).endswith("@" + digest) for value in record.get("repo_digests", []))):
            raise BuildProvenanceError("Runtime dependency evidence disagrees with the lock", {"label": label})
        approved_external[reference] = str(record["image_id"])
    findings: List[Dict[str, Any]] = []
    offenders: Dict[str, str] = {}
    for container, observation in running.items():
        if isinstance(observation, Mapping):
            image_id = str(observation.get("image") or "")
            image_reference = str(observation.get("image_reference") or "")
        else:
            image_id = str(observation)
            image_reference = ""
        from_this_build = image_id in built_ids
        pinned_external = (
            bool(image_reference)
            and approved_external.get(image_reference) == image_id
        )
        findings.append({
            "container": container,
            "image_id": image_id,
            "image_reference": image_reference,
            "from_this_build": from_this_build,
            "pinned_runtime_dependency": pinned_external,
        })
        if not from_this_build and not pinned_external:
            offenders[container] = image_id
    if offenders:
        raise BuildProvenanceError(
            "Containers are running images that this build did not produce. The selected "
            "sources are therefore not what is being tested.",
            {
                "offenders": offenders,
                "built_image_ids": sorted(built_ids),
                "approved_runtime_images": approved_external,
            },
        )
    return findings


def record_tool_versions(
    manifest: BuildManifest,
    *,
    runner: Optional[CommandRunner] = None,
    probes: Sequence[Tuple[str, Sequence[str]]] = (),
) -> None:
    """Record the real versions of the tools used by this run."""
    exec_runner = runner or _default_runner
    default_probes: Tuple[Tuple[str, Sequence[str]], ...] = (
        ("git", ("git", "--version")),
        ("python", (sys.executable, "--version")),
        ("docker", ("docker", "--version")),
        ("go", ("go", "version")),
        ("cargo", ("cargo", "--version")),
        ("cosmwasm-check", ("cosmwasm-check", "--version")),
        ("java", ("java", "-version")),
        ("node", ("node", "--version")),
    )
    for name, argv in (probes or default_probes):
        proc = exec_runner(list(argv), timeout=120.0)
        stdout = (getattr(proc, "stdout", "") or "").strip()
        stderr = (getattr(proc, "stderr", "") or "").strip()
        manifest.tools.append(
            {
                "name": name,
                "argv": list(argv),
                "exit_code": getattr(proc, "returncode", None),
                "version": redact(stdout or stderr),
            }
        )


__all__ = [
    "Builder",
    "CommandRunner",
    "IMAGE_LEDGER_FILENAME",
    "IMAGE_LEDGER_SCHEMA",
    "ImageObservation",
    "PROTECTED_SOURCE_ROLES",
    "image_ledger_path",
    "ledger_fingerprints",
    "ledger_is_damaged",
    "ledger_key",
    "load_image_ledger",
    "observe_runtime",
    "parse_version_long",
    "record_tool_versions",
    "source_fingerprint",
    "staged_tree_digest",
    "step_source_roles",
    "verify_observed_runtime",
    "verify_running_images",
]
