#!/usr/bin/env bash
# scripts/lakebase-down.sh
#
# Tear down the Lakebase endpoint after tests.
#
# Default: disables the compute endpoint (IDLE state, zero compute cost, data
# preserved).  The next run of lakebase-up.sh re-enables it automatically.
#
# Pass --destroy to permanently delete the entire project and all data.
#
# Note: the primary read-write endpoint of a branch cannot be deleted via the
# CLI; disabling it is the correct way to stop compute.
#
# Auth
# ----
# Uses the same DATABRICKS_PROFILE as lakebase-up.sh (default: DEFAULT).
#
# Usage
# -----
#   ./scripts/lakebase-down.sh            # disable endpoint (default)
#   ./scripts/lakebase-down.sh --destroy  # delete project + all data

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROFILE="${DATABRICKS_PROFILE:-DEFAULT}"
PROJECT_ID="${LAKEBASE_PROJECT_ID:-statschema-test}"
ENV_FILE="${LAKEBASE_ENV_FILE:-.env}"
DESTROY=false

for arg in "$@"; do
    case "$arg" in
        --destroy) DESTROY=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

PROFILE_FLAG=()
if [ "$PROFILE" != "DEFAULT" ]; then
    PROFILE_FLAG=(-p "$PROFILE")
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info() { echo "  [lakebase-down] $*" >&2; }
die()  { echo "  [lakebase-down] ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' not found."
}

env_get() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

require_cmd databricks

# ---------------------------------------------------------------------------
# Disable endpoint (default) or destroy project
# ---------------------------------------------------------------------------

if $DESTROY; then
    info "WARNING: --destroy will permanently delete all Lakebase data for projects/${PROJECT_ID}."
    if [ -t 0 ]; then
        read -rp "  [lakebase-down] Type YES to confirm: " CONFIRM
        [ "$CONFIRM" = "YES" ] || { info "Aborted."; exit 0; }
    else
        info "  (non-interactive — proceeding with --destroy)"
    fi
    info "Deleting project: projects/${PROJECT_ID}"
    databricks postgres delete-project "projects/${PROJECT_ID}" "${PROFILE_FLAG[@]}" 2>&1 \
        | sed 's/^/    /' >&2 || info "  (project already gone — OK)"
    info "Project deleted."
    exit 0
fi

# Default: disable the endpoint so compute stops (data is preserved).
ENDPOINT_NAME=$(env_get "STATSCHEMA_LAKEBASE_ENDPOINT")
if [ -z "$ENDPOINT_NAME" ]; then
    info "STATSCHEMA_LAKEBASE_ENDPOINT not set in ${ENV_FILE} — nothing to disable."
    exit 0
fi

info "Disabling endpoint (compute stops, data preserved): ${ENDPOINT_NAME}"
databricks postgres update-endpoint "$ENDPOINT_NAME" \
    "spec.disabled" --json '{"spec":{"disabled":true}}' \
    -o json "${PROFILE_FLAG[@]}" >/dev/null 2>&1 \
    && info "Endpoint disabled (IDLE).  Re-run 'make lakebase-up' to re-enable." \
    || info "  (disable failed — endpoint may already be gone)"
