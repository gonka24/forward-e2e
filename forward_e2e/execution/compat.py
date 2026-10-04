"""Compatibility adapters: how a *specific* selected source tree is driven.

Why adapters exist
------------------
The harness historically hard-pinned two Gonka SHAs and blanket-copied the
whole ``gonka-overlay`` on top of any checkout. That was safe for exactly those
two commits and wrong for anything else: the overlay replaced
``inference-chain/app/legacy.go``, which is precisely the file upstream
changed in ``e86e4899bd8cf52d1ad4766c811f65230b2f9296``
("feat(wasm): expose epoch, reward, and vesting queries to contracts (#1758)").
Copying an old production file over a newer upstream implementation tests the
overlay instead of upstream.

The immutable-source model removes the overlay altogether. The selected Gonka
commit is built and tested exactly as it is; tests, network configuration and
container control live in the runner (``harness/``) and never enter the
source snapshot. An adapter therefore declares, per supported family:

* **family markers** that are *measured from the acquired target sources*, not
  guessed from a SHA allow-list. A pinned SHA list exists only to say "this
  exact commit has been reviewed", never as the sole matching mechanism;
* a **build recipe** that produces the real chain images/binaries from the
  selected sources with explicitly recorded arguments, and whose every output
  and staged build context lives *outside* the snapshots;
* **runtime expectations** that are themselves derived from the target sources
  (the selected commit itself, the ``wasmd``/``wasmvm`` versions read out of the
  target's own ``go.mod``) and then compared against the *running* binary;
* which scenarios it supports, and an explicit reason for every scenario it
  does not.

There is deliberately no way to declare a patch, an allow-listed changed path
or a "prepared" commit: a family that cannot be tested unmodified is an
unsupported family, reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .errors import UnsupportedCompatibilityError
from .runlock import content_sha256


# ---------------------------------------------------------------------------
# marketplace query surface
# ---------------------------------------------------------------------------
#: The four chain queries the marketplace contracts need from inside the Wasm
#: VM. Each entry lists alternative textual fingerprints: the gRPC path literal
#: and the generated response type name. A tree "exposes" the capability when
#: *any* alternative is present, which keeps the marker robust against upstream
#: refactoring the literal into a constant.
MARKETPLACE_QUERY_CAPABILITIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "current_epoch",
        (
            "/inference.inference.Query/GetCurrentEpoch",
            "QueryGetCurrentEpochResponse",
        ),
    ),
    (
        "claim_recipients",
        (
            "/inference.inference.Query/ListClaimRecipients",
            "QueryListClaimRecipientsResponse",
        ),
    ),
    (
        "epoch_performance_summary",
        (
            "/inference.inference.Query/EpochPerformanceSummaryByParticipant",
            "QueryEpochPerformanceSummaryByParticipantResponse",
        ),
    ),
    (
        "total_vesting",
        (
            "/inference.streamvesting.Query/TotalVestingAmount",
            "QueryTotalVestingAmountResponse",
        ),
    ),
)

WASM_ALLOWLIST_FILE = "inference-chain/app/legacy.go"

# ---------------------------------------------------------------------------
# markers measured from the target tree
# ---------------------------------------------------------------------------
class MarkerKind(str, Enum):
    FILE_EXISTS = "file_exists"
    FILE_ABSENT = "file_absent"
    DIR_EXISTS = "dir_exists"
    CONTAINS_ALL = "contains_all"
    CONTAINS_ANY = "contains_any"
    CONTAINS_NONE = "contains_none"
    CAPABILITIES_PRESENT = "capabilities_present"


@dataclass(frozen=True)
class SourceMarker:
    """One measurable statement about the acquired source tree."""

    path: str
    kind: MarkerKind
    needles: Tuple[str, ...] = ()
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "needles": list(self.needles),
            "why": self.why,
        }

    def evaluate(self, root: Path) -> Tuple[bool, str]:
        target = Path(root) / self.path if self.path else Path(root)
        if self.kind is MarkerKind.DIR_EXISTS:
            return (target.is_dir(), f"{self.path} is not a directory")
        if self.kind is MarkerKind.FILE_EXISTS:
            return (target.is_file(), f"{self.path} is missing")
        if self.kind is MarkerKind.FILE_ABSENT:
            return (not target.exists(), f"{self.path} unexpectedly exists")

        if not target.is_file():
            return (False, f"{self.path} is missing, so it cannot be inspected")
        text = target.read_text(encoding="utf-8", errors="replace")

        if self.kind is MarkerKind.CONTAINS_ALL:
            missing = [n for n in self.needles if n not in text]
            return (not missing, f"{self.path} does not contain {missing}")
        if self.kind is MarkerKind.CONTAINS_ANY:
            return (
                any(n in text for n in self.needles),
                f"{self.path} contains none of {list(self.needles)}",
            )
        if self.kind is MarkerKind.CONTAINS_NONE:
            present = [n for n in self.needles if n in text]
            return (not present, f"{self.path} unexpectedly contains {present}")
        if self.kind is MarkerKind.CAPABILITIES_PRESENT:
            exposed = [
                name
                for name, alternatives in MARKETPLACE_QUERY_CAPABILITIES
                if any(alt in text for alt in alternatives)
            ]
            all_names = [name for name, _ in MARKETPLACE_QUERY_CAPABILITIES]
            missing = [n for n in all_names if n not in exposed]
            return (
                not missing,
                f"{self.path} does not expose the marketplace queries {missing}",
            )
        return (False, f"unknown marker kind {self.kind}")


def measured_capabilities(root: Path) -> List[str]:
    """Which marketplace queries the target tree already exposes natively."""
    allowlist = Path(root) / WASM_ALLOWLIST_FILE
    if not allowlist.is_file():
        return []
    text = allowlist.read_text(encoding="utf-8", errors="replace")
    return [
        name
        for name, alternatives in MARKETPLACE_QUERY_CAPABILITIES
        if any(alt in text for alt in alternatives)
    ]


def measure_go_module_version(root: Path, go_mod_relpath: str, module: str) -> Optional[str]:
    """Read a dependency version out of the *target's own* ``go.mod``.

    This is how runtime expectations stay correct for an arbitrary SHA: the
    expected ``wasmd``/``wasmvm`` version is not a constant baked into the
    runner, it is measured from the sources being tested and then compared
    against what the running binary reports.
    """
    path = Path(root) / go_mod_relpath
    if not path.is_file():
        return None
    # Track directive blocks so exclude/replace entries cannot masquerade as
    # required dependencies. Comments are legal on both forms of require.
    block = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line:
            continue
        if line == ")":
            block = None
            continue
        opening = re.fullmatch(r"(\w+)\s*\(", line)
        if opening:
            block = opening.group(1)
            continue
        fields = line.split()
        if block is None and fields[0] == "require":
            fields = fields[1:]
        elif block != "require":
            continue
        if len(fields) == 2 and fields[0] == module:
            return fields[1]
    return None


# ---------------------------------------------------------------------------
# build recipes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageCopy:
    """One byte-identical copy of source content into a staged build context.

    Why this exists: upstream ``make build-docker`` builds the chain image from
    ``inference-chain/.docker-context``, a directory it creates *inside the
    checkout*. Running that target would write into the snapshot. The runner
    reproduces the copy itself, into a directory under the build output, and
    hands that directory to ``docker build``. The copy is performed by the
    builder in-process (never a shell glob) and its content digest is recorded,
    so a reviewer can see exactly which bytes the image was built from.
    """

    source: str
    destination: str
    what: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "destination": self.destination, "what": self.what}


@dataclass(frozen=True)
class BuildStep:
    """One command (or one in-process staging action) of a build recipe.

    ``argv``, ``env`` and the staging paths may contain ``{gonka}``,
    ``{contracts}``, ``{runner}``, ``{build_out}``, ``{platform}``,
    ``{goarch}`` and the version placeholders listed in
    :data:`BUILD_CONTEXT_KEYS`, substituted with real values at execution time.
    The resolved argv is recorded in the build manifest so a reviewer can
    confirm that the build really pointed at the selected sources.

    A step with ``stage`` entries and an empty ``argv`` runs no process: the
    builder copies each entry and records the digest of what it staged.
    ``build_args`` is the explicit ``--build-arg`` set of a ``docker build``
    step, kept as data (in addition to appearing in ``argv``) so the manifest
    states every value without anyone having to parse a command line.
    """

    step_id: str
    argv: Tuple[str, ...]
    cwd_role: str
    timeout_seconds: int
    description: str
    env: Mapping[str, str] = field(default_factory=dict)
    produces_images: Tuple[str, ...] = ()
    produces_files: Tuple[str, ...] = ()
    stage: Tuple[StageCopy, ...] = ()
    build_args: Tuple[Tuple[str, str], ...] = ()

    @property
    def is_stage(self) -> bool:
        return bool(self.stage) and not self.argv

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "argv": list(self.argv),
            "cwd_role": self.cwd_role,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
            "env": dict(self.env),
            "produces_images": list(self.produces_images),
            "produces_files": list(self.produces_files),
            "stage": [copy.to_dict() for copy in self.stage],
            "build_args": [[name, value] for name, value in self.build_args],
        }


@dataclass(frozen=True)
class BuildRecipe:
    recipe_id: str
    steps: Tuple[BuildStep, ...]
    external_images: Mapping[str, str]
    toolchain: Mapping[str, str]
    chain_images: Tuple[str, ...]
    #: Third-party services the test network needs but which are not built from
    #: the selected Gonka source. Values are immutable registry references;
    #: they are pulled for the locked platform before the suite starts.
    runtime_external_images: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "steps": [s.to_dict() for s in self.steps],
            "external_images": dict(self.external_images),
            "runtime_external_images": dict(self.runtime_external_images),
            "toolchain": dict(self.toolchain),
            "chain_images": list(self.chain_images),
        }

    def fingerprint(self) -> str:
        """Content hash of the whole recipe; part of every image's reuse proof."""
        return content_sha256(self.to_dict())


# ---------------------------------------------------------------------------
# runtime expectations
# ---------------------------------------------------------------------------
class ExpectationKind(str, Enum):
    #: The running binary must report the *selected* commit itself. Nothing is
    #: patched, so there is no other commit it could legitimately report.
    SELECTED_COMMIT = "selected_commit"
    #: The value must equal a version measured from the target's own go.mod.
    SOURCE_GO_MODULE = "source_go_module"
    #: The value must match a regular expression pinned by the adapter.
    LITERAL_PATTERN = "literal_pattern"


@dataclass(frozen=True)
class RuntimeExpectation:
    component: str
    field_name: str
    kind: ExpectationKind
    go_mod_relpath: Optional[str] = None
    go_module: Optional[str] = None
    pattern: Optional[str] = None
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "field_name": self.field_name,
            "kind": self.kind.value,
            "go_mod_relpath": self.go_mod_relpath,
            "go_module": self.go_module,
            "pattern": self.pattern,
            "why": self.why,
        }

    def expected_value(self, *, sources_root: Path, selected_sha: Optional[str]) -> Optional[str]:
        if self.kind is ExpectationKind.SELECTED_COMMIT:
            return selected_sha
        if self.kind is ExpectationKind.SOURCE_GO_MODULE:
            if not self.go_mod_relpath or not self.go_module:
                return None
            return measure_go_module_version(sources_root, self.go_mod_relpath, self.go_module)
        return None


@dataclass(frozen=True)
class HarnessBinding:
    """How the runner's harness must be parameterised for this family.

    The harness itself always comes from the runner image. Only the values it
    needs to know about the *target* are supplied here.
    """

    version_probe_argv: Tuple[str, ...]
    version_commit_field: str
    node_container: str
    compose_project_names: Tuple[str, ...]
    genesis_script: str
    chain_home: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_probe_argv": list(self.version_probe_argv),
            "version_commit_field": self.version_commit_field,
            "node_container": self.node_container,
            "compose_project_names": list(self.compose_project_names),
            "genesis_script": self.genesis_script,
            "chain_home": self.chain_home,
        }


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompatibilityAdapter:
    adapter_id: str
    role: str
    description: str
    verified_commits: FrozenSet[str]
    markers: Tuple[SourceMarker, ...]
    build: Optional[BuildRecipe] = None
    runtime: Tuple[RuntimeExpectation, ...] = ()
    harness: Optional[HarnessBinding] = None
    supported_scenarios: FrozenSet[str] = frozenset()
    unsupported_scenarios: Mapping[str, str] = field(default_factory=dict)
    toolchain_requirements: Mapping[str, str] = field(default_factory=dict)
    limitations: Tuple[str, ...] = ()

    #: Stated in every declaration and lock record so a reader never has to
    #: infer it from the absence of a patch list.
    SOURCE_POLICY = "immutable"

    # -- identity ------------------------------------------------------
    def declaration(self) -> Dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "role": self.role,
            "description": self.description,
            "source_policy": self.SOURCE_POLICY,
            "verified_commits": sorted(self.verified_commits),
            "markers": [m.to_dict() for m in self.markers],
            "build": self.build.to_dict() if self.build else None,
            "runtime": [r.to_dict() for r in self.runtime],
            "harness": self.harness.to_dict() if self.harness else None,
            "supported_scenarios": sorted(self.supported_scenarios),
            "unsupported_scenarios": dict(self.unsupported_scenarios),
            "toolchain_requirements": dict(self.toolchain_requirements),
            "limitations": list(self.limitations),
        }

    def content_hash(self) -> str:
        return content_sha256(self.declaration())

    def lock_record(self, *, match_mode: str, measurements: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_content_hash": self.content_hash(),
            "role": self.role,
            "match_mode": match_mode,
            "measurements": dict(measurements),
            "source_policy": self.SOURCE_POLICY,
            "build_recipe": self.build.to_dict() if self.build else None,
            "build_recipe_fingerprint": self.build.fingerprint() if self.build else None,
            "runtime_expectations": [r.to_dict() for r in self.runtime],
            "harness": self.harness.to_dict() if self.harness else None,
            "supported_scenarios": sorted(self.supported_scenarios),
            "unsupported_scenarios": dict(self.unsupported_scenarios),
            "limitations": list(self.limitations),
        }

    # -- matching ------------------------------------------------------
    def evaluate_markers(self, root: Path) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        for marker in self.markers:
            ok, why = marker.evaluate(root)
            if not ok:
                reasons.append(why if not marker.why else f"{why} ({marker.why})")
        return (not reasons, reasons)


# ---------------------------------------------------------------------------
# concrete adapters
# ---------------------------------------------------------------------------
_ALL_NATIVE_SCENARIOS = frozenset(
    {
        "funded-claim",
        "no-buyer-claim-expiry",
        "no-sale-vesting-lifecycle",
        "emergency-host-only-recovery",
        "funded-routing-refunds",
        "unfunded-lock-boundaries",
        "funded-gas-sweep",
        "network-unconfirmed",
        "claim-expiry-positive",
        "claim-expiry-zero",
        "terminal-release-repeat",
        "foreign-native-preservation",
        "late-donation-after-completed",
        "lock-exact-e",
        "lock-e-plus-4",
        "lock-e-plus-5",
        "refund-boundary-and-vesting-addition",
        "usdt-withdrawal-failure-recovery",
        "native-release-rollback-retry",
    }
)

_BOUNDARY_SCENARIOS = frozenset(
    {
        "go-query-error-classification",
        "wasm-abi-boundary",
        "contract-network-unconfirmed-policy",
        "contract-claim-expiry-policy",
        "contract-query-fault-policy",
    }
)

_HARNESS_BINDING = HarnessBinding(
    version_probe_argv=("inferenced", "version", "--long"),
    version_commit_field="commit",
    node_container="genesis-node",
    compose_project_names=("genesis", "join1", "join2", "testdns"),
    genesis_script="inference-chain/scripts/init-docker-genesis.sh",
    chain_home="/root/.inference",
)

_COMMON_GONKA_MARKERS = (
    SourceMarker(
        path=WASM_ALLOWLIST_FILE,
        kind=MarkerKind.FILE_EXISTS,
        why="the wasm query allow-list lives here",
    ),
    SourceMarker(path="inference-chain/Dockerfile", kind=MarkerKind.FILE_EXISTS),
    SourceMarker(path="decentralized-api", kind=MarkerKind.DIR_EXISTS),
    SourceMarker(path="testermint/gradlew", kind=MarkerKind.FILE_EXISTS),
    SourceMarker(path="local-test-net", kind=MarkerKind.DIR_EXISTS),
    SourceMarker(path="inference-chain/go.mod", kind=MarkerKind.FILE_EXISTS),
    SourceMarker(path="Makefile", kind=MarkerKind.FILE_EXISTS),
)

_COMMON_RUNTIME_EXPECTATIONS = (
    RuntimeExpectation(
        component="inference-chain",
        field_name="gonka_source_sha",
        kind=ExpectationKind.SELECTED_COMMIT,
        why=(
            "the running binary must report exactly the selected commit: it was built from "
            "that commit's unmodified tree, so any other value means another binary is running"
        ),
    ),
    RuntimeExpectation(
        component="inference-chain",
        field_name="wasmd",
        kind=ExpectationKind.SOURCE_GO_MODULE,
        go_mod_relpath="inference-chain/go.mod",
        go_module="github.com/CosmWasm/wasmd",
        why="measured from the selected sources instead of a constant baked into the runner",
    ),
    RuntimeExpectation(
        component="inference-chain",
        field_name="wasmvm",
        kind=ExpectationKind.SOURCE_GO_MODULE,
        go_mod_relpath="inference-chain/go.mod",
        go_module="github.com/CosmWasm/wasmvm/v2",
        why="measured from the selected sources instead of a constant baked into the runner",
    ),
)

_CHAIN_IMAGES = (
    "ghcr.io/product-science/inferenced:latest",
    "ghcr.io/product-science/api:latest",
    "ghcr.io/product-science/edge-api:latest",
    "edge-api:latest",
    "ghcr.io/product-science/proxy:latest",
    "inference-mock-server:latest",
)

#: Context keys the executor supplies to every recipe. ``gonka_version`` is
#: ``git describe --always`` measured read-only in the snapshot, exactly the
#: value upstream's Makefiles compute for ``VERSION``.
BUILD_CONTEXT_KEYS = (
    "gonka", "contracts", "runner", "build_out", "platform", "goarch",
    "gonka_sha", "gonka_version", "contracts_sha", "runner_image_id", "python",
)

#: Where the external Gradle init script puts the mock server's build
#: directory, relative to ``{build_out}``. The init script
#: (``harness/testermint/gradle/out-of-tree.init.gradle.kts``) sets
#: the root project's ``buildDirectory`` to ``<a8.outRoot>/<rootProject.name>``;
#: upstream's ``rootProject.name`` for the mock server is ``mock_server``.
#: Kept as one constant so a change of the init-script layout is one edit.
MOCK_SERVER_OUT_ROOT = "mock-server-build"
MOCK_SERVER_JAR_RELPATH = f"{MOCK_SERVER_OUT_ROOT}/mock_server/libs/mock_server-all.jar"

#: The path upstream's ``testermint/Dockerfile`` COPYs the jar from, relative
#: to its build context.
MOCK_SERVER_DOCKERFILE_JAR = "mock_server/build/libs/mock_server-all.jar"

#: Staged Docker build contexts, relative to ``{build_out}``. Never inside a
#: snapshot: that is the whole point of staging them.
INFERENCED_CONTEXT_RELPATH = "docker-context/inferenced"
MOCK_SERVER_CONTEXT_RELPATH = "docker-context/mock-server"

_COSMOS_VERSION_PKG = "github.com/cosmos/cosmos-sdk/version"


def _upstream_ldflags(name: str, app_name: str) -> str:
    """``ldflags`` exactly as ``inference-chain/Makefile`` / ``decentralized-api/Makefile``.

    At c33c9eaa both define::

        ldflags = -X .../version.Name=<name> -X .../version.AppName=<app> \\
                  -X .../version.Version=$(VERSION) -X .../version.Commit=$(COMMIT)

    with ``COMMIT := $(shell git log -1 --format='%H')`` (the selected commit,
    since the snapshot's HEAD is that commit) and ``VERSION ?= $(shell git
    describe --always)``. Make joins the continuation lines with single spaces.
    """
    return " ".join(
        (
            f"-X {_COSMOS_VERSION_PKG}.Name={name}",
            f"-X {_COSMOS_VERSION_PKG}.AppName={app_name}",
            f"-X {_COSMOS_VERSION_PKG}.Version={{gonka_version}}",
            f"-X {_COSMOS_VERSION_PKG}.Commit={{gonka_sha}}",
        )
    )


def _docker_build(
    *,
    platform: str,
    dockerfile: str,
    context_dir: str,
    tags: Sequence[str],
    build_args: Sequence[Tuple[str, str]] = (),
) -> Tuple[str, ...]:
    argv: List[str] = ["docker", "build", "--platform", platform]
    for name, value in build_args:
        argv += ["--build-arg", f"{name}={value}"]
    argv += ["-f", dockerfile]
    for tag in tags:
        argv += ["-t", tag]
    argv.append(context_dir)
    return tuple(argv)


#: ``DOCKER_BUILDKIT=1 docker build`` is upstream's ``DOCKER_BUILD_PREFIX``
#: when no registry cache is used.
_BUILDKIT_ENV = {"DOCKER_BUILDKIT": "1"}


def _gonka_build_recipe(recipe_id: str) -> BuildRecipe:
    """Build the real network images from the *selected* Gonka sources.

    This reproduces, from runner code, the calls upstream's root
    ``make build-docker`` makes for the images the test network uses
    (``node-``, ``api-``, ``edge-api-``, ``proxy-`` and
    ``mock-server-build-docker`` at c33c9eaa). ``make`` itself is not run
    because two of those targets write into the checkout:

    * ``inference-chain``'s ``DOCKER_BUILD`` creates ``.docker-context`` inside
      the tree (and ``rm -rf build/``). Here the same context -- a copy of
      ``inference-chain/`` plus ``cosmovisor/`` -- is staged under
      ``{build_out}`` and handed to ``docker build``.
    * ``mock-server-build-docker`` runs ``./gradlew clean shadowJar`` inside
      ``testermint/mock_server``. Here the Gradle wrapper jar is invoked
      directly (never the ``gradlew`` shell script, whose line endings the old
      runner had to "fix" with a copy inside the tree) with an external init
      script and project cache that send every output under ``{build_out}``;
      the jar is then staged next to upstream's Dockerfile layout.

    The api/edge-api/proxy images use the repository root or their own
    directory as a read-only build context: a build context is sent to the
    daemon, never written to or mounted.

    Every ``--build-arg`` is explicit and recorded. ``BLST_PORTABLE=1`` and the
    test genesis overrides file are the values the previous runner passed to
    ``make``; ``GOOS``/``GOARCH``/``--platform`` follow the locked platform
    except for the api image, whose upstream target pins ``linux/amd64``.
    """
    inferenced_context = "{build_out}/" + INFERENCED_CONTEXT_RELPATH
    mock_context = "{build_out}/" + MOCK_SERVER_CONTEXT_RELPATH

    inferenced_args = (
        ("LDFLAGS", _upstream_ldflags("inference-chain", "inferenced")),
        ("GOOS", "linux"),
        ("GOARCH", "{goarch}"),
        ("BLST_PORTABLE", "1"),
        ("GENESIS_OVERRIDES_FILE", "inference-chain/test_genesis_overrides.json"),
    )
    # decentralized-api/Makefile `build-docker` forces PLATFORM/GOOS/GOARCH to
    # linux/amd64 via $(eval ...) regardless of what the root Makefile passes.
    # Reproducing upstream faithfully means keeping that pin. DEVSHARD_VERSION
    # is not exported by the root Makefile, so the api Makefile's own default
    # (`DEVSHARD_VERSION ?= $(VERSION)`) applies.
    api_args = (
        ("LDFLAGS", _upstream_ldflags("decentralized-api", "decentralized-api")),
        ("GOOS", "linux"),
        ("GOARCH", "amd64"),
        ("BLST_PORTABLE", "1"),
        ("DEVSHARD_VERSION", "{gonka_version}"),
    )
    edge_args = (
        ("GOOS", "linux"),
        ("GOARCH", "{goarch}"),
        ("BLST_PORTABLE", "1"),
    )

    steps: List[BuildStep] = [
        BuildStep(
            step_id="stage-inferenced-context",
            argv=(),
            cwd_role="build_out",
            timeout_seconds=15 * 60,
            description=(
                "Stage the inference-chain Docker context outside the snapshot: a byte-identical "
                "copy of inference-chain/ and cosmovisor/, as upstream's DOCKER_BUILD would "
                "create in inference-chain/.docker-context."
            ),
            stage=(
                StageCopy(
                    source="{gonka}/inference-chain",
                    destination=inferenced_context + "/inference-chain",
                    what="inference-chain sources",
                ),
                StageCopy(
                    source="{gonka}/cosmovisor",
                    destination=inferenced_context + "/cosmovisor",
                    what="pinned cosmovisor binaries",
                ),
            ),
        ),
        BuildStep(
            step_id="inferenced-image",
            argv=_docker_build(
                platform="{platform}",
                dockerfile="{gonka}/inference-chain/Dockerfile",
                context_dir=inferenced_context,
                tags=(
                    "ghcr.io/product-science/inferenced:{gonka_version}",
                    "ghcr.io/product-science/inferenced:latest",
                ),
                build_args=inferenced_args,
            ),
            cwd_role="build_out",
            timeout_seconds=60 * 60,
            description="Build the inferenced image from the staged, unmodified sources.",
            env=_BUILDKIT_ENV,
            produces_images=("ghcr.io/product-science/inferenced:latest",),
            build_args=inferenced_args,
        ),
        BuildStep(
            step_id="api-image",
            argv=_docker_build(
                platform="linux/amd64",
                dockerfile="{gonka}/decentralized-api/Dockerfile",
                context_dir="{gonka}",
                tags=(
                    "ghcr.io/product-science/api:{gonka_version}",
                    "ghcr.io/product-science/api:latest",
                ),
                build_args=api_args,
            ),
            cwd_role="build_out",
            timeout_seconds=60 * 60,
            description="Build the decentralized-api image (read-only repository-root context).",
            env=_BUILDKIT_ENV,
            produces_images=("ghcr.io/product-science/api:latest",),
            build_args=api_args,
        ),
        BuildStep(
            step_id="edge-api-image",
            argv=_docker_build(
                platform="{platform}",
                dockerfile="{gonka}/edge-api/Dockerfile",
                context_dir="{gonka}",
                tags=(
                    "ghcr.io/product-science/edge-api:{gonka_version}",
                    "ghcr.io/product-science/edge-api:latest",
                    "edge-api:latest",
                ),
                build_args=edge_args,
            ),
            cwd_role="build_out",
            timeout_seconds=45 * 60,
            description="Build the edge-api image (read-only repository-root context).",
            env=_BUILDKIT_ENV,
            produces_images=("ghcr.io/product-science/edge-api:latest", "edge-api:latest"),
            build_args=edge_args,
        ),
        BuildStep(
            step_id="proxy-image",
            argv=_docker_build(
                platform="{platform}",
                dockerfile="{gonka}/proxy/Dockerfile",
                context_dir="{gonka}/proxy",
                tags=(
                    "ghcr.io/product-science/proxy:{gonka_version}",
                    "ghcr.io/product-science/proxy:latest",
                ),
            ),
            cwd_role="build_out",
            timeout_seconds=20 * 60,
            description="Build the proxy image (read-only proxy/ context).",
            env=_BUILDKIT_ENV,
            produces_images=("ghcr.io/product-science/proxy:latest",),
        ),
        BuildStep(
            step_id="mock-server-jar",
            argv=(
                "java",
                "-cp", "{gonka}/testermint/mock_server/gradle/wrapper/gradle-wrapper.jar",
                "org.gradle.wrapper.GradleWrapperMain",
                "--no-daemon",
                "--project-dir", "{gonka}/testermint/mock_server",
                "--init-script",
                "{runner}/harness/testermint/gradle/out-of-tree.init.gradle.kts",
                "--project-cache-dir", "{build_out}/gradle-project-cache/mock-server",
                "-Pa8.outRoot={build_out}/" + MOCK_SERVER_OUT_ROOT,
                "-Pkotlin.project.persistent.dir={build_out}/kotlin/mock-server",
                "shadowJar",
            ),
            cwd_role="build_out",
            timeout_seconds=30 * 60,
            description=(
                "Build the mock server jar with the upstream Gradle wrapper; every build, cache "
                "and Kotlin output is redirected under the build directory."
            ),
            env={"GRADLE_USER_HOME": "{build_out}/gradle-home"},
            produces_files=(MOCK_SERVER_JAR_RELPATH,),
        ),
        BuildStep(
            step_id="stage-mock-server-context",
            argv=(),
            cwd_role="build_out",
            timeout_seconds=5 * 60,
            description=(
                "Stage the mock server Docker context outside the snapshot: the built jar at the "
                "path upstream's testermint/Dockerfile copies it from."
            ),
            stage=(
                StageCopy(
                    source="{build_out}/" + MOCK_SERVER_JAR_RELPATH,
                    destination=mock_context + "/" + MOCK_SERVER_DOCKERFILE_JAR,
                    what="mock server jar built from the selected commit",
                ),
            ),
        ),
        BuildStep(
            step_id="mock-server-image",
            argv=_docker_build(
                platform="{platform}",
                dockerfile="{gonka}/testermint/Dockerfile",
                context_dir=mock_context,
                tags=("inference-mock-server:latest",),
            ),
            cwd_role="build_out",
            timeout_seconds=20 * 60,
            description="Build the mock server image with upstream's Dockerfile.",
            env=_BUILDKIT_ENV,
            produces_images=("inference-mock-server:latest",),
        ),
    ]
    return BuildRecipe(
        recipe_id=recipe_id,
        steps=tuple(steps),
        external_images={
            # Pinned by VERSIONS.md for the released marketplace contracts.
            "cosmwasm-optimizer": (
                "cosmwasm/optimizer:0.16.1@sha256:"
                "b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e"
            ),
            # Pinned by the runner's ``harness/wasm_query_allowlist`` probe (formerly the
            # gonka-overlay p0-probe Makefile).
            "cosmwasm-optimizer-probe": (
                "cosmwasm/optimizer:0.17.0@sha256:"
                "7e0b9229c1a4118d0c9a2af2e7f5d95a91f264c26a2ce5681c779926e74d7f85"
            ),
        },
        toolchain={
            "rust": "1.81.0",
            "cosmwasm-check": "2.2.2",
            "java": "21",
            "node": "22.23.3",
            "go": "1.26.8",
        },
        chain_images=_CHAIN_IMAGES,
        # Support services selected by Gonka's local-test-net compose files,
        # not artefacts built from the selected Gonka commit. The tag documents
        # the compose contract; the registry index digest pins every platform.
        runtime_external_images={
            "postgres": (
                "postgres:18.1-bookworm@sha256:"
                "cc9f4143a8d2fa8cf3749d0cb4d26ecf2d53a77a2ac807e9ebd67ae22426221a"
            ),
            "test-dns": (
                "coredns/coredns:1.11.1@sha256:"
                "1eeb4c7316bacb1d4c8ead65571cd92dd21e27359f0d4917f1a5822a73b75db1"
            ),
        },
    )


def gonka_immutable_source_adapter() -> CompatibilityAdapter:
    """Gonka families that already expose the marketplace queries upstream.

    Verified against ``c33c9eaa5bc40c53b564159b5e1534bbfdab8a08`` and
    ``e86e4899bd8cf52d1ad4766c811f65230b2f9296`` ("feat(wasm): expose epoch,
    reward, and vesting queries to contracts (#1758)").

    The selected commit is tested exactly as it is. Nothing from the runner is
    written into the tree; the marketplace scenarios, network configuration and
    container control live in ``harness/``.
    """
    return CompatibilityAdapter(
        adapter_id="gonka-immutable-source-v2",
        role="gonka",
        description=(
            "Gonka family whose own legacy.go already exposes the marketplace epoch, reward "
            "and vesting queries to contracts. The selected commit is built and tested "
            "without any modification of its source tree."
        ),
        verified_commits=frozenset(
            {
                "c33c9eaa5bc40c53b564159b5e1534bbfdab8a08",
                "e86e4899bd8cf52d1ad4766c811f65230b2f9296",
            }
        ),
        markers=_COMMON_GONKA_MARKERS
        + (
            SourceMarker(
                path=WASM_ALLOWLIST_FILE,
                kind=MarkerKind.CAPABILITIES_PRESENT,
                why="the upstream allow-list must already expose all four marketplace queries",
            ),
            SourceMarker(path="cosmovisor", kind=MarkerKind.DIR_EXISTS,
                         why="staged into the inferenced build context"),
            SourceMarker(path="edge-api/Dockerfile", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(path="proxy/Dockerfile", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(path="decentralized-api/Dockerfile", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(path="testermint/Dockerfile", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(
                path="testermint/mock_server/gradle/wrapper/gradle-wrapper.jar",
                kind=MarkerKind.FILE_EXISTS,
                why="the mock server is built by invoking the upstream wrapper jar directly",
            ),
        ),
        build=_gonka_build_recipe("gonka-immutable-images-v2"),
        runtime=_COMMON_RUNTIME_EXPECTATIONS,
        harness=_HARNESS_BINDING,
        supported_scenarios=_ALL_NATIVE_SCENARIOS | _BOUNDARY_SCENARIOS,
        unsupported_scenarios={},
        toolchain_requirements={"go": ">=1.23", "java": "21", "docker": "compose-v2"},
        limitations=(
            "The marketplace scenarios are compiled against the selected Testermint as a "
            "dependency. If upstream removed an API they use, the external harness fails "
            "before the network starts; that is a real compatibility finding, not a runner "
            "defect, and it is never patched around.",
        ),
    )


def marketplace_contracts_adapter() -> CompatibilityAdapter:
    """The contracts workspace this runner knows how to build.

    Notably it does **not** require ``forward_e2e`` or ``scripts/acceptance_harness.py``
    in the target: the harness, the catalog and the verifier always come from
    the runner image. A public export such as ``gonka24-forward`` that ships
    the contracts and the packages but no runner modules is
    therefore a perfectly valid target.
    """
    return CompatibilityAdapter(
        adapter_id="contracts-marketplace-workspace-v1",
        role="contracts",
        description=(
            "Cargo workspace holding the marketplace contracts and the test packages, "
            "built by the runner-owned reproducible A9 release script."
        ),
        verified_commits=frozenset({"7497304e5dc6bf48accdd8c91549bc22de6997fc"}),
        markers=(
            SourceMarker(path="Cargo.toml", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(
                path="Cargo.toml",
                kind=MarkerKind.CONTAINS_ANY,
                needles=("[workspace]",),
                why="the release build operates on the whole workspace",
            ),
            SourceMarker(path="Cargo.lock", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(path="rust-toolchain.toml", kind=MarkerKind.FILE_EXISTS),
            SourceMarker(path="contracts", kind=MarkerKind.DIR_EXISTS),
            SourceMarker(path="packages", kind=MarkerKind.DIR_EXISTS),
        ),
        build=BuildRecipe(
            recipe_id="contracts-a9-release-v1",
            steps=(
                BuildStep(
                    step_id="a9-release",
                    argv=(
                        "{python}", "{runner}/vendor/contract_release/release.py", "build",
                        "--repo", "{contracts}",
                        "--commit", "{contracts_sha}",
                        "--output", "{build_out}/a9-release",
                    ),
                    cwd_role="contracts",
                    timeout_seconds=45 * 60,
                    description=(
                        "Reproducible double build of the production contracts from the "
                        "selected contracts commit, validated with cosmwasm-check."
                    ),
                    # The manifest alone is only a claim. Declaring the Wasm
                    # themselves makes the builder hash the actual production
                    # binaries into the build manifest, which is what lets a
                    # reviewer check offline that the artefacts deployed into
                    # the network are the ones this build produced.
                    produces_files=(
                        "a9-release/build-manifest.json",
                        "a9-release/wasm/marketplace_deal.wasm",
                        "a9-release/wasm/marketplace_factory.wasm",
                    ),
                ),
                BuildStep(
                    step_id="test-contracts",
                    argv=(
                        "cargo", "build", "-p", "a8-caller", "-p", "a8-cw20",
                        "--release", "--target", "wasm32-unknown-unknown",
                        "--target-dir", "{build_out}/test-wasm-target", "--locked",
                    ),
                    cwd_role="contracts",
                    timeout_seconds=20 * 60,
                    description="Build the harness-side test contracts from the same commit.",
                    produces_files=(
                        "test-wasm-target/wasm32-unknown-unknown/release/a8_caller.wasm",
                        "test-wasm-target/wasm32-unknown-unknown/release/a8_cw20.wasm",
                    ),
                ),
            ),
            external_images={
                "cosmwasm-optimizer": (
                    "cosmwasm/optimizer:0.16.1@sha256:"
                    "b9c92b2900b7ebaab3499203615c1b8589592bc557355ed3432e48851ffde69e"
                ),
            },
            toolchain={"rust": "1.81.0", "cosmwasm-check": "2.2.2"},
            chain_images=(),
        ),
        runtime=(),
        harness=None,
        supported_scenarios=_ALL_NATIVE_SCENARIOS | _BOUNDARY_SCENARIOS,
        unsupported_scenarios={},
        toolchain_requirements={"rust": "1.81.0", "cosmwasm-check": "2.2.2"},
        limitations=(
            "The Wasm hashes recorded in the build manifest come from the A9 double build of "
            "the selected contracts commit; a cached artefact is never substituted.",
        ),
    )


def gonka_adapters() -> Tuple[CompatibilityAdapter, ...]:
    return (gonka_immutable_source_adapter(),)


def contracts_adapters() -> Tuple[CompatibilityAdapter, ...]:
    return (marketplace_contracts_adapter(),)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdapterMatch:
    adapter: CompatibilityAdapter
    match_mode: str
    measurements: Dict[str, Any]


def select_adapter(
    candidates: Sequence[CompatibilityAdapter],
    *,
    root: Path,
    commit_sha: str,
    role: str,
) -> AdapterMatch:
    """Choose the adapter whose measured markers all hold.

    The commit allow-list only upgrades ``match_mode`` from ``markers`` to
    ``verified-commit``; it never selects on its own, and a commit that is not
    in any allow-list is still fully supported as long as the measured markers
    match. That is what makes "any explicitly pinned SHA of the development
    repository" work without touching the runner.
    """
    rejections: Dict[str, List[str]] = {}
    for adapter in candidates:
        ok, reasons = adapter.evaluate_markers(root)
        if ok:
            return AdapterMatch(
                adapter=adapter,
                match_mode=(
                    "verified-commit" if commit_sha.lower() in adapter.verified_commits
                    else "markers"
                ),
                measurements={
                    "exposed_marketplace_queries": measured_capabilities(root)
                    if role == "gonka" else [],
                    "evaluated_markers": [m.to_dict() for m in adapter.markers],
                },
            )
        rejections[adapter.adapter_id] = reasons

    raise UnsupportedCompatibilityError(
        f"No compatibility adapter can drive the selected {role} sources. The runner will not "
        "guess a build recipe for an unknown layout.",
        {
            "role": role,
            "commit_sha": commit_sha,
            "rejected": rejections,
            "hint": (
                "Add a compatibility adapter describing this family, or select a commit whose "
                "layout one of the existing adapters recognises."
            ),
        },
    )


def assert_scenarios_supported(
    adapter: CompatibilityAdapter,
    scenarios: Sequence[str],
    *,
    selection_label: str,
) -> None:
    """Refuse a selection containing a scenario this family cannot run.

    ``--profile all`` must never turn green by silently dropping a task, so an
    unsupported scenario is a hard error naming every offender and the reason.
    """
    offenders = {}
    for scenario in scenarios:
        if scenario in adapter.unsupported_scenarios:
            offenders[scenario] = adapter.unsupported_scenarios[scenario]
        elif scenario not in adapter.supported_scenarios:
            offenders[scenario] = "not declared as supported by this compatibility adapter"
    if offenders:
        raise UnsupportedCompatibilityError(
            f"The selection {selection_label!r} includes scenarios that the compatibility "
            f"adapter {adapter.adapter_id!r} cannot run. They are not skipped: choose a "
            "different selection or a different source combination.",
            {
                "adapter_id": adapter.adapter_id,
                "unsupported": offenders,
                "selection": list(scenarios),
            },
        )


__all__ = [
    "AdapterMatch",
    "BUILD_CONTEXT_KEYS",
    "BuildRecipe",
    "BuildStep",
    "CompatibilityAdapter",
    "ExpectationKind",
    "HarnessBinding",
    "INFERENCED_CONTEXT_RELPATH",
    "MARKETPLACE_QUERY_CAPABILITIES",
    "MOCK_SERVER_CONTEXT_RELPATH",
    "MOCK_SERVER_JAR_RELPATH",
    "MarkerKind",
    "RuntimeExpectation",
    "SourceMarker",
    "StageCopy",
    "WASM_ALLOWLIST_FILE",
    "assert_scenarios_supported",
    "contracts_adapters",
    "gonka_adapters",
    "gonka_immutable_source_adapter",
    "marketplace_contracts_adapter",
    "measure_go_module_version",
    "measured_capabilities",
    "select_adapter",
]
