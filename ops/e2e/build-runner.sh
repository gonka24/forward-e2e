#!/usr/bin/env bash
# =============================================================================
# ops/e2e/build-runner.sh -- one-time bootstrap of the tools container.
#
# This builds ONLY the runner image: the container that holds Git, Docker CE,
# Go, Rust, cosmwasm-check, Java and Node, plus the versioned harness, catalog
# and verifier. It does not build, fetch or run anything from a target
# repository, and it never starts the inner Docker daemon.
#
# After this has been run once, every acceptance run is a single command:
#   ./ops/e2e/run-e2e.sh run --gonka-repo ... --gonka-sha ... \
#                            --contracts-repo ... --contracts-sha ... --profile all
#
# Nothing beyond Docker is required on the host.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/ops/runner/compose.yaml"
IMAGE="${E2E_RUNNER_IMAGE:-a8-runner:local}"
RUNNER_SHA="${E2E_RUNNER_SHA:-}"
RUNNER_REPO="${E2E_RUNNER_REPO:-https://github.com/gonka24/forward-e2e.git}"

die() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

while [ "$#" -gt 0 ]; do
    [ "$#" -ge 2 ] || die "Missing value for $1" 2
    case "$1" in
        --runner-sha) RUNNER_SHA="$2" ;;
        --runner-repo) RUNNER_REPO="$2" ;;
        --image) IMAGE="$2" ;;
        *) die "Unknown build argument: $1" 2 ;;
    esac
    shift 2
done
[[ "$RUNNER_SHA" =~ ^[0-9a-f]{40}$ ]] || die "--runner-sha requires a full lowercase 40-hex commit SHA." 2
[[ "$RUNNER_REPO" == https://* && "$RUNNER_REPO" != *'#'* ]] || die "--runner-repo requires an HTTPS Git URL without a revision fragment." 2
export E2E_RUNNER_SHA="$RUNNER_SHA" E2E_RUNNER_REPO="$RUNNER_REPO"

command -v docker >/dev/null 2>&1 || die "Docker is required on the host but was not found."

compose() {
    # Compose V2 only; see ops/e2e/run-e2e.sh for why the retired Compose v1
    # binary cannot parse ops/runner/compose.yaml.
    docker compose version >/dev/null 2>&1 \
        || die "Docker Compose V2 ('docker compose') is required on the host but was not found."
    docker compose -f "$COMPOSE_FILE" "$@"
}

export E2E_RUNNER_IMAGE="$IMAGE"
# The build context is the repository root, so these only satisfy compose's
# variable interpolation for bind sources that the build itself never reads.
export GONKA_DIR="${GONKA_DIR:-$REPO_ROOT}"
export CONTRACTS_DIR="${CONTRACTS_DIR:-$REPO_ROOT}"
export OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/out}"
export E2E_PLAN_DIR="${E2E_PLAN_DIR:-$REPO_ROOT}"
export E2E_SECRETS_DIR="${E2E_SECRETS_DIR:-$REPO_ROOT}"
mkdir -p -- "$OUTPUT_DIR"

printf 'Building the E2E runner image %s ...\n' "$IMAGE"
printf '  runner SHA: %s\n' "$RUNNER_SHA"
compose build e2e-runner || die "Runner image build failed." 1

IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")" \
    || die "The image was built but could not be inspected." 1
DIGEST="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$IMAGE_ID" 2>/dev/null || true)"

printf '\nRunner image ready.\n'
printf '  locator : %s\n' "$IMAGE"
printf '  image id: %s\n' "$IMAGE_ID"
if [ -n "$DIGEST" ]; then
    printf '  digest  : %s (retrieval hint; publication not verified)\n' "$DIGEST"
else
    printf '  digest  : (none - local image only)\n'
    printf '            A plan created with this image can only be replayed on a host that\n'
    printf '            has this exact image. Transfer it with docker save | docker load;\n'
    printf '            a rebuild from the same tag produces a different image and is refused.\n'
fi
printf '\nNext: ./ops/e2e/run-e2e.sh list\n'
