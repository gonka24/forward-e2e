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
COMPOSE_FILE="${REPO_ROOT}/ops/a8/compose.yaml"
IMAGE="${E2E_RUNNER_IMAGE:-a8-runner:local}"

die() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

command -v docker >/dev/null 2>&1 || die "Docker is required on the host but was not found."

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE" "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose -f "$COMPOSE_FILE" "$@"
    else
        die "Neither 'docker compose' nor 'docker-compose' is available."
    fi
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
