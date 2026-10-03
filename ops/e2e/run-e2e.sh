#!/usr/bin/env bash
# =============================================================================
# ops/e2e/run-e2e.sh -- public E2E entry point for Linux and macOS.
#
# This wrapper is deliberately thin. It does not parse the acceptance CLI: it
# forwards arguments verbatim to the single parser inside the runner container
# (forward_e2e/execution/cli.py), so the documented examples and the implementation can
# never drift apart.
#
# It does exactly four host-side jobs, because they cannot be done from inside
# the container:
#
#   1. resolve the runner image tag to an immutable image id and digest, and
#      pass them in, so the run records the identity it actually used;
#   2. translate host paths (--gonka-path, --contracts-path, --output, --from,
#      --run, --credential-file) into container paths and bind them;
#   3. bridge a local Git repository -- including a Git worktree, whose .git is
#      a pointer to a host path that does not exist in the container -- into a
#      self-contained bare repository holding the requested commit;
#   4. check every exit code on the way.
#
# A remote-only run (--gonka-repo/--contracts-repo) needs nothing but Docker:
# no host Git, no Python, no Java, no Rust, no Go, no WSL preparation.
#
# Usage:
#   ./ops/e2e/run-e2e.sh list
#   ./ops/e2e/run-e2e.sh plan --gonka-repo ... --gonka-sha ... \
#       --contracts-repo ... --contracts-sha ... --profile all --output ./out/plan
#   ./ops/e2e/run-e2e.sh run --from ./out/plan/run.lock.json
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/ops/runner/compose.yaml"
SERVICE="e2e-runner"

die() {
    printf 'error: %s\n' "$1" >&2
    exit "${2:-2}"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' is required on the host but was not found."
}

abs_path() {
    # Works for paths that contain spaces and for paths that do not exist yet.
    local target="$1"
    if [ -d "$target" ]; then
        (cd -- "$target" && pwd)
    else
        local parent base
        parent="$(dirname -- "$target")"
        base="$(basename -- "$target")"
        [ -d "$parent" ] || die "Directory does not exist: ${parent}"
        printf '%s/%s' "$(cd -- "$parent" && pwd)" "$base"
    fi
}

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose -f "$COMPOSE_FILE" "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose -f "$COMPOSE_FILE" "$@"
    else
        die "Neither 'docker compose' nor 'docker-compose' is available."
    fi
}

require_cmd docker

# Normalize only options consumed by this wrapper; other arguments remain the
# container parser's responsibility. Do this before SHA discovery as well.
NORMALIZED=()
for token in "$@"; do
    case "$token" in
        --gonka-sha=* | --contracts-sha=* | --gonka-path=* | --contracts-path=* | \
        --output=* | --run=* | --from=* | --credential-file=* | \
        --runner-image=* | --docker-root-volume=*)
            NORMALIZED+=("${token%%=*}" "${token#*=}")
            ;;
        *) NORMALIZED+=("$token") ;;
    esac
done
# The conditional expansion also works with empty arrays on macOS Bash 3.2.
set -- ${NORMALIZED[@]+"${NORMALIZED[@]}"}

# -----------------------------------------------------------------------------
# First pass: read the values the wrapper itself needs, without consuming them.
# -----------------------------------------------------------------------------
GONKA_SHA=""
CONTRACTS_SHA=""
prev=""
for token in "$@"; do
    case "$prev" in
        --gonka-sha)     GONKA_SHA="$token" ;;
        --contracts-sha) CONTRACTS_SHA="$token" ;;
    esac
    prev="$token"
done

# -----------------------------------------------------------------------------
# Bridge a local repository into a self-contained bare repository.
#
# Never touches the caller's repository: it only reads objects out of it. A
# dirty working tree and a different checked-out HEAD are therefore irrelevant
# by construction, and no uncommitted file can enter the snapshot.
# -----------------------------------------------------------------------------
BRIDGE_ROOT="${E2E_BRIDGE_DIR:-${REPO_ROOT}/.e2e-bridge}"
BRIDGE_SESSION=""
BRIDGE_ROOT_ID=""
BRIDGE_SESSION_ID=""

bridge_identity() {
    # GNU and BSD/macOS stat use different option names. Include both device
    # and inode so an identically named replacement cannot inherit ownership.
    stat -c '%d:%i' -- "$1" 2>/dev/null || stat -f '%d:%i' "$1" 2>/dev/null
}

cleanup_bridge() {
    if [ -n "$BRIDGE_SESSION" ]; then
        local current_root_id current_session_id
        if [ "$(dirname -- "$BRIDGE_SESSION")" != "$BRIDGE_ROOT" ] ||
           [ -L "$BRIDGE_ROOT" ] || [ -L "$BRIDGE_SESSION" ] ||
           [ ! -d "$BRIDGE_ROOT" ] || [ ! -d "$BRIDGE_SESSION" ] ||
           ! current_root_id="$(bridge_identity "$BRIDGE_ROOT")" ||
           ! current_session_id="$(bridge_identity "$BRIDGE_SESSION")" ||
           [ "$current_root_id" != "$BRIDGE_ROOT_ID" ] ||
           [ "$current_session_id" != "$BRIDGE_SESSION_ID" ]; then
            printf 'warning: bridge directory identity changed; preserving it for manual inspection.\n' >&2
            return
        fi
        rm -rf -- "$BRIDGE_SESSION"
    fi
}
trap cleanup_bridge EXIT

prepare_bridge_session() {
    # Allocate in the parent shell so EXIT cleanup also covers a failed fetch.
    # Concurrent planners do not share the container's execution flock.
    if [ -z "$BRIDGE_SESSION" ]; then
        [ ! -L "$BRIDGE_ROOT" ] || die "Bridge root must not be a symlink: ${BRIDGE_ROOT}"
        mkdir -p -- "$BRIDGE_ROOT"
        [ ! -L "$BRIDGE_ROOT" ] || die "Bridge root must not be a symlink: ${BRIDGE_ROOT}"
        BRIDGE_ROOT="$(cd -- "$BRIDGE_ROOT" && pwd -P)"
        BRIDGE_ROOT_ID="$(bridge_identity "$BRIDGE_ROOT")" \
            || die "Could not record the bridge root identity." 1
        BRIDGE_SESSION="$(mktemp -d "${BRIDGE_ROOT}/run.XXXXXXXX")" \
            || die "Could not create the bridge session." 1
        BRIDGE_SESSION_ID="$(bridge_identity "$BRIDGE_SESSION")" \
            || die "Could not record the bridge session identity." 1
    fi
}

bridge_local_repo() {
    local role="$1" src="$2" sha="$3"

    [ -n "$sha" ] || die "--${role}-path requires --${role}-sha with a full 40-hex commit SHA."
    case "$sha" in
        *[!0-9a-fA-F]* | "") die "--${role}-sha must be a full 40-hex commit SHA; got '${sha}'." ;;
    esac
    [ "${#sha}" -eq 40 ] || die "--${role}-sha must be exactly 40 hex characters; got '${sha}'."
    require_cmd git

    local bridge
    bridge="$(mktemp -d "${BRIDGE_SESSION}/${role}.XXXXXXXX")" \
        || die "Failed to allocate the host bridge repository." 1

    git init --bare --quiet -- "$bridge" \
        || die "Failed to create the host bridge repository at ${bridge}." 1

    # Objects only. --no-tags keeps the mirror small; the explicit tag refspec
    # brings tag objects that a commit may need.
    if ! git -C "$bridge" fetch --no-tags --quiet -- "$src" \
        '+refs/heads/*:refs/e2e/heads/*' '+refs/tags/*:refs/e2e/tags/*'; then
        die "Failed to read Git objects from '${src}'. Is it a Git repository?" 1
    fi

    # A commit that is not reachable from any ref (for example one that only a
    # detached worktree points at) still has to work, so try a direct object
    # fetch as well. Its failure is not fatal on its own; the object-type check
    # below is what decides.
    git -C "$bridge" fetch --no-tags --quiet -- "$src" "$sha" >/dev/null 2>&1 || true

    local object_type
    if ! object_type="$(git -C "$bridge" cat-file -t "$sha" 2>/dev/null)"; then
        die "Commit ${sha} was not found in '${src}'. No other revision is substituted." 1
    fi
    if [ "$object_type" != "commit" ]; then
        die "${sha} in '${src}' is a ${object_type}, not a commit." 1
    fi
    if ! git -C "$bridge" update-ref "refs/e2e/source/${role}" "$sha"; then
        die "Failed to record ${sha} in the host bridge repository." 1
    fi

    printf '%s' "$bridge"
}

# -----------------------------------------------------------------------------
# Second pass: rewrite host paths and strip wrapper-only flags.
# -----------------------------------------------------------------------------
FORWARD=()
OUTPUT_DIR_HOST=""
PLAN_DIR_HOST=""
SECRETS_DIR_HOST=""
GONKA_BRIDGE=""
CONTRACTS_BRIDGE=""
RUN_ARG_INDEX=-1
RUN_ARG_VALUE=""
RUNNER_IMAGE="${E2E_RUNNER_IMAGE:-a8-runner:local}"
RUNNER_IMAGE_EXPLICIT=false
if [ -n "${E2E_DOCKER_ROOT_VOLUME:-}" ] && [ -n "${A8_DOCKER_ROOT_VOLUME:-}" ] && [ "$E2E_DOCKER_ROOT_VOLUME" != "$A8_DOCKER_ROOT_VOLUME" ]; then
    die "Conflicting environment variables E2E_DOCKER_ROOT_VOLUME='${E2E_DOCKER_ROOT_VOLUME}' and A8_DOCKER_ROOT_VOLUME='${A8_DOCKER_ROOT_VOLUME}'; unset the legacy A8_DOCKER_ROOT_VOLUME variable or set both to the same value." 2
fi
DOCKER_ROOT_VOLUME="${E2E_DOCKER_ROOT_VOLUME:-${A8_DOCKER_ROOT_VOLUME:-a8-docker-root}}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --runner-image)
            [ "$#" -ge 2 ] || die "--runner-image requires a value."
            RUNNER_IMAGE="$2"
            RUNNER_IMAGE_EXPLICIT=true
            shift 2
            ;;
        --docker-root-volume)
            [ "$#" -ge 2 ] || die "--docker-root-volume requires a value."
            DOCKER_ROOT_VOLUME="$2"
            case "$DOCKER_ROOT_VOLUME" in
                '' | *[!A-Za-z0-9_.-]* | [-_.]*)
                    die "--docker-root-volume must be a plain Docker volume name; got '${DOCKER_ROOT_VOLUME}'."
                    ;;
            esac
            [ "${#DOCKER_ROOT_VOLUME}" -le 128 ] \
                || die "--docker-root-volume must be at most 128 characters."
            shift 2
            ;;
        --gonka-path)
            [ "$#" -ge 2 ] || die "--gonka-path requires a value."
            prepare_bridge_session
            GONKA_BRIDGE="$(bridge_local_repo gonka "$(abs_path "$2")" "$GONKA_SHA")"
            FORWARD+=("--gonka-path" "/input/gonka")
            shift 2
            ;;
        --contracts-path)
            [ "$#" -ge 2 ] || die "--contracts-path requires a value."
            prepare_bridge_session
            CONTRACTS_BRIDGE="$(bridge_local_repo contracts "$(abs_path "$2")" "$CONTRACTS_SHA")"
            FORWARD+=("--contracts-path" "/input/contracts")
            shift 2
            ;;
        --output)
            [ "$#" -ge 2 ] || die "--output requires a value."
            mkdir -p -- "$2"
            OUTPUT_DIR_HOST="$(abs_path "$2")"
            FORWARD+=("--output" "/out")
            shift 2
            ;;
        --run)
            # report/recover take a run directory or a bare run id. The value is
            # only translated after the loop, because --output may come later on
            # the command line and it decides what /out is.
            [ "$#" -ge 2 ] || die "--run requires a run directory or a run id."
            RUN_ARG_VALUE="$2"
            FORWARD+=("--run")
            RUN_ARG_INDEX="${#FORWARD[@]}"
            FORWARD+=("$2")
            shift 2
            ;;
        --from)
            [ "$#" -ge 2 ] || die "--from requires a path to run.lock.json."
            [ -e "$2" ] || die "Plan not found: $2"
            if [ -d "$2" ]; then
                PLAN_DIR_HOST="$(abs_path "$2")"
                FORWARD+=("--from" "/input/plan/run.lock.json")
            else
                PLAN_DIR_HOST="$(cd -- "$(dirname -- "$2")" && pwd)"
                FORWARD+=("--from" "/input/plan/$(basename -- "$2")")
            fi
            shift 2
            ;;
        --credential-file)
            [ "$#" -ge 2 ] || die "--credential-file requires a value."
            [ -f "$2" ] || die "Credential file not found: $2"
            SECRETS_DIR_HOST="$(cd -- "$(dirname -- "$2")" && pwd)"
            FORWARD+=("--credential-file" "/run/secrets/e2e/$(basename -- "$2")")
            shift 2
            ;;
        *)
            FORWARD+=("$1")
            shift
            ;;
    esac
done

# This host-consumed option is removed from FORWARD, so the container cannot enforce
# its semantic-override rule. Preserve that rule here before launching anything.
if [ -n "$PLAN_DIR_HOST" ] && [ "$RUNNER_IMAGE_EXPLICIT" = true ]; then
    die "--from executes a saved plan exactly; --runner-image cannot be combined with --from. Create a new plan instead."
fi

# -----------------------------------------------------------------------------
# Translate --run. A bare run id is meaningful inside the container as it is; a
# host path is not, so it is rewritten to the same location under /out. When the
# caller gave no --output, the output root is inferred from the documented
# layout (<output>/runs/<run-id>) so that `report`/`recover` work with nothing
# but the run directory that `run` printed.
# -----------------------------------------------------------------------------
if [ "$RUN_ARG_INDEX" -ge 0 ]; then
    case "$RUN_ARG_VALUE" in
        */* | . | ..)
            [ -d "$RUN_ARG_VALUE" ] \
                || die "Run directory not found: ${RUN_ARG_VALUE}
Pass the directory that \`run\` created, or the bare run id together with the --output that holds it."
            RUN_DIR_HOST="$(abs_path "$RUN_ARG_VALUE")"
            if [ -z "$OUTPUT_DIR_HOST" ]; then
                # Match the PowerShell wrapper: keep the enclosing package's
                # lock and manifests inside the mount when given a nested suite.
                # Markers only choose a mount; the container validates evidence.
                PACKAGE_ROOT="$RUN_DIR_HOST"
                CANDIDATE="$RUN_DIR_HOST"
                for ((depth = 0; depth <= 6; depth++)); do
                    FOUND_PACKAGE=false
                    for marker in run.lock.json execution-manifest.json build-manifest.json delivery.json e2e-run-result.json; do
                        if [ -f "$CANDIDATE/$marker" ]; then
                            FOUND_PACKAGE=true
                            break
                        fi
                    done
                    if [ "$FOUND_PACKAGE" = true ]; then
                        PACKAGE_ROOT="$CANDIDATE"
                        break
                    fi
                    PARENT="$(dirname -- "$CANDIDATE")"
                    [ "$PARENT" != "$CANDIDATE" ] || break
                    CANDIDATE="$PARENT"
                done
                RUN_PARENT="$(dirname -- "$PACKAGE_ROOT")"
                if [ "$(basename -- "$RUN_PARENT")" = "runs" ]; then
                    OUTPUT_DIR_HOST="$(dirname -- "$RUN_PARENT")"
                else
                    OUTPUT_DIR_HOST="$RUN_PARENT"
                fi
            fi
            if [ "$RUN_DIR_HOST" = "$OUTPUT_DIR_HOST" ]; then
                FORWARD[$RUN_ARG_INDEX]="/out"
            else
                case "$RUN_DIR_HOST" in
                    "$OUTPUT_DIR_HOST"/*)
                        FORWARD[$RUN_ARG_INDEX]="/out/${RUN_DIR_HOST#"${OUTPUT_DIR_HOST}"/}"
                        ;;
                    *)
                        die "--run ${RUN_DIR_HOST} is outside --output ${OUTPUT_DIR_HOST}.
Only one host directory is mounted for evidence, so both must live under it.
Either drop --output, or pass the --output that contains this run."
                        ;;
                esac
            fi
            ;;
        *)
            # A bare id: the container resolves it against /out and /workspace.
            ;;
    esac
fi

# -----------------------------------------------------------------------------
# Resolve the runner image to an immutable identity.
# -----------------------------------------------------------------------------
if ! IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$RUNNER_IMAGE" 2>/dev/null)"; then
    die "The runner image '${RUNNER_IMAGE}' is not available on this host.
Build it once with:
  ./ops/e2e/build-runner.sh
or pass --runner-image with an image that is present." 1
fi
IMAGE_DIGEST="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$IMAGE_ID" 2>/dev/null || true)"

if [[ "$RUNNER_IMAGE" != *@sha256:* && ! "$RUNNER_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf 'note: runner image locator %s is a mutable tag; it currently resolves to %s\n' \
        "$RUNNER_IMAGE" "$IMAGE_ID" >&2
fi
if [ -z "$IMAGE_DIGEST" ]; then
    printf 'note: this runner image has no registry digest. Replaying a plan created with it on another host requires transferring the image (docker save | docker load); a rebuild from the same tag produces a different image and is refused.\n' >&2
fi

# -----------------------------------------------------------------------------
# Compose environment. Unset bind sources fall back to harmless read-only paths.
# -----------------------------------------------------------------------------
# Compose must launch the inspected image even if its tag moves meanwhile.
# The container receives the original locator via the explicit environment
# override below, preserving the locator recorded in provenance.
export E2E_RUNNER_IMAGE="$IMAGE_ID"
export E2E_RUNNER_IMAGE_ID="$IMAGE_ID"
export E2E_RUNNER_IMAGE_DIGEST="$IMAGE_DIGEST"
export GONKA_DIR="${GONKA_BRIDGE:-$REPO_ROOT}"
export CONTRACTS_DIR="${CONTRACTS_BRIDGE:-$REPO_ROOT}"
export OUTPUT_DIR="${OUTPUT_DIR_HOST:-${REPO_ROOT}/out}"
export E2E_PLAN_DIR="${PLAN_DIR_HOST:-$REPO_ROOT}"
export E2E_SECRETS_DIR="${SECRETS_DIR_HOST:-$REPO_ROOT}"
export E2E_DOCKER_ROOT_VOLUME="$DOCKER_ROOT_VOLUME"
export A8_DOCKER_ROOT_VOLUME="$DOCKER_ROOT_VOLUME"
mkdir -p -- "$OUTPUT_DIR"

compose run --rm -e "E2E_RUNNER_IMAGE=$RUNNER_IMAGE" "$SERVICE" ${FORWARD[@]+"${FORWARD[@]}"}
