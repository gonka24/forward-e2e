#!/usr/bin/env bash
# ==============================================================================
# Container entrypoint for the E2E explicit-SHA acceptance runner.
#
# Differences from the historical a8-entrypoint:
#   * it dispatches to forward_e2e.execution.cli, the single E2E parser;
#   * it trusts runner-created repositories under /workspace and /out, plus
#     the explicit input and application paths listed below;
#   * input repositories are mounted read-only. Git safe.directory controls
#     ownership trust, not write permissions; the mounts prevent source writes.
# ==============================================================================
set -euo pipefail

# Specific safe directories only. A global '*' is deliberately not used.
git config --system --add safe.directory /input/marketplace 2>/dev/null || true
git config --system --add safe.directory /input/gonka 2>/dev/null || true
git config --system --add safe.directory /input/contracts 2>/dev/null || true
git config --system --add safe.directory /app 2>/dev/null || true
git config --system --add safe.directory '/workspace/*' 2>/dev/null || true
git config --system --add safe.directory '/out/*' 2>/dev/null || true

# Never prompt for credentials: a private source must use the explicit
# credential provider, which feeds the secret through an askpass helper.
export GIT_TERMINAL_PROMPT=0

# Ensure the application root is on sys.path even when invoked from another cwd.
if [ -n "${E2E_APP_ROOT:-}" ] && [ -n "${A8_APP_ROOT:-}" ] && [ "$E2E_APP_ROOT" != "$A8_APP_ROOT" ]; then
    printf 'error: conflicting environment variables E2E_APP_ROOT=%s and A8_APP_ROOT=%s; unset the legacy A8_APP_ROOT variable or set both to the same value.\n' "$E2E_APP_ROOT" "$A8_APP_ROOT" >&2
    exit 2
fi
APP_ROOT="${E2E_APP_ROOT:-${A8_APP_ROOT:-/app}}"
cd "$APP_ROOT" || {
    printf 'error: the runner application root %s is missing from this image.\n' "$APP_ROOT" >&2
    exit 1
}
export PYTHONPATH="${APP_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m forward_e2e.execution.cli "$@"
