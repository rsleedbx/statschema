#!/usr/bin/env bash
# scripts/lakebase-up.sh
#
# Spin up the smallest Databricks Lakebase endpoint for statschema live tests.
# Idempotent: reuses an existing project + endpoint if their names are already
# saved in .env.  Only creates resources that do not yet exist.
#
# Auth
# ----
# Reads workspace credentials from ~/.databrickscfg.
# Uses the DEFAULT profile unless DATABRICKS_PROFILE is set.
#
# Outputs (written to .env; printed to stdout as export statements)
# -----------------------------------------------------------
#   STATSCHEMA_LAKEBASE_ENDPOINT  full resource path
#   STATSCHEMA_LAKEBASE_HOST      PostgreSQL hostname
#   STATSCHEMA_LAKEBASE_DB        database name (databricks_postgres)
#   STATSCHEMA_LAKEBASE_USER      Databricks user name or SP client ID
#
# Usage
# -----
#   ./scripts/lakebase-up.sh               # uses DEFAULT profile
#   DATABRICKS_PROFILE=ci ./scripts/lakebase-up.sh
#   source <(./scripts/lakebase-up.sh)     # export vars into current shell

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROFILE="${DATABRICKS_PROFILE:-DEFAULT}"
PROJECT_ID="${LAKEBASE_PROJECT_ID:-statschema-test}"
ENDPOINT_ID="primary"
DB_NAME="${STATSCHEMA_LAKEBASE_DB:-databricks_postgres}"
ENV_FILE="${LAKEBASE_ENV_FILE:-.env}"

# -p flag — omit for DEFAULT (CLI picks it up automatically)
PROFILE_FLAG=()
if [ "$PROFILE" != "DEFAULT" ]; then
    PROFILE_FLAG=(-p "$PROFILE")
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info() { echo "  [lakebase-up] $*" >&2; }
die()  { echo "  [lakebase-up] ERROR: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' not found.  $2"
}

# Write or update KEY=VALUE in the env file.
env_set() {
    local key="$1" val="$2"
    if [ ! -f "$ENV_FILE" ]; then touch "$ENV_FILE"; fi
    if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i.bak "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" && rm -f "${ENV_FILE}.bak"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

# Read KEY from the env file (empty string if absent).
env_get() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

require_cmd databricks "Install the Databricks CLI: https://docs.databricks.com/aws/en/dev-tools/cli/install"
require_cmd jq          "Install jq: https://jqlang.github.io/jq/download/"

info "Profile : ${PROFILE}"
info "Project : projects/${PROJECT_ID}"

# ---------------------------------------------------------------------------
# Determine PGUSER
# Prefer explicit env var, then DATABRICKS_CLIENT_ID (service principal),
# then fall back to the logged-in user's email.
# ---------------------------------------------------------------------------

PG_USER=""
if [ -n "${STATSCHEMA_LAKEBASE_USER:-}" ]; then
    PG_USER="$STATSCHEMA_LAKEBASE_USER"
elif [ -n "${DATABRICKS_CLIENT_ID:-}" ]; then
    PG_USER="$DATABRICKS_CLIENT_ID"
else
    PG_USER=$(databricks current-user me -o json "${PROFILE_FLAG[@]}" 2>/dev/null \
              | jq -r '.userName // empty' 2>/dev/null || true)
    if [ -z "$PG_USER" ]; then
        info "WARNING: could not determine Databricks user."
        info "  Set STATSCHEMA_LAKEBASE_USER to your user email or service-principal UUID."
    fi
fi
[ -n "$PG_USER" ] && info "User    : ${PG_USER}"

# ---------------------------------------------------------------------------
# Check whether a saved endpoint already exists (idempotency)
# ---------------------------------------------------------------------------

SAVED_ENDPOINT=""
SAVED_ENDPOINT=$(env_get "STATSCHEMA_LAKEBASE_ENDPOINT")

PG_HOST=""
ENDPOINT_NAME=""

if [ -n "$SAVED_ENDPOINT" ]; then
    info "Found saved endpoint: ${SAVED_ENDPOINT}"
    EP_JSON=$(databricks postgres get-endpoint "$SAVED_ENDPOINT" \
              -o json "${PROFILE_FLAG[@]}" 2>/dev/null || true)

    if echo "$EP_JSON" | jq -e '.name' >/dev/null 2>&1; then
        ENDPOINT_NAME="$SAVED_ENDPOINT"
        IS_DISABLED=$(echo "$EP_JSON" | jq -r '.status.disabled // false')
        if [ "$IS_DISABLED" = "true" ]; then
            info "Endpoint is disabled — re-enabling…"
            databricks postgres update-endpoint "$ENDPOINT_NAME" \
                "spec.disabled" --json '{"spec":{"disabled":false}}' \
                -o json "${PROFILE_FLAG[@]}" >/dev/null
            EP_JSON=$(databricks postgres get-endpoint "$ENDPOINT_NAME" \
                      -o json "${PROFILE_FLAG[@]}")
        else
            info "Endpoint is alive — nothing to create."
        fi
        PG_HOST=$(echo "$EP_JSON" | jq -r '.status.hosts.host')
    else
        info "Saved endpoint not found in Databricks — recreating."
        SAVED_ENDPOINT=""
    fi
fi

# ---------------------------------------------------------------------------
# Create / discover resources (only when endpoint is not already alive)
# ---------------------------------------------------------------------------

if [ -z "$SAVED_ENDPOINT" ]; then

    # ── 1. Create project ────────────────────────────────────────────────────
    # create-project also creates the default production branch and primary
    # endpoint automatically.  The command exits non-zero if the project already
    # exists, which is fine — we continue in that case.
    info "Creating project 'projects/${PROJECT_ID}' (no-op if it already exists)…"
    databricks postgres create-project "$PROJECT_ID" \
        --json "{\"spec\":{\"display_name\":\"statschema integration tests\"}}" \
        -o json "${PROFILE_FLAG[@]}" >/dev/null 2>&1 \
        || info "  (project already exists — continuing)"

    # ── 2. Get default branch ────────────────────────────────────────────────
    info "Fetching default branch…"
    BRANCH_NAME=$(databricks postgres list-branches "projects/${PROJECT_ID}" \
        -o json "${PROFILE_FLAG[@]}" \
        | jq -r '.[] | select(.status.default == true) | .name')
    [ -n "$BRANCH_NAME" ] || die "No default branch found for projects/${PROJECT_ID}"
    info "  Branch: ${BRANCH_NAME}"

    # ── 3. Find or create the primary endpoint ───────────────────────────────
    ENDPOINT_NAME="${BRANCH_NAME}/endpoints/${ENDPOINT_ID}"
    EP_JSON=$(databricks postgres get-endpoint "$ENDPOINT_NAME" \
              -o json "${PROFILE_FLAG[@]}" 2>/dev/null || true)

    if ! echo "$EP_JSON" | jq -e '.name' >/dev/null 2>&1; then
        info "Creating endpoint '${ENDPOINT_ID}' (min=0.5, max=0.5 CU)…"
        databricks postgres create-endpoint "${BRANCH_NAME}" "${ENDPOINT_ID}" \
            --json '{"spec":{"endpoint_type":"ENDPOINT_TYPE_READ_WRITE","autoscaling_limit_min_cu":0.5,"autoscaling_limit_max_cu":0.5}}' \
            -o json "${PROFILE_FLAG[@]}" >/dev/null
        EP_JSON=$(databricks postgres get-endpoint "$ENDPOINT_NAME" \
                  -o json "${PROFILE_FLAG[@]}")
    else
        # Endpoint exists.  If it was disabled by lakebase-down.sh, re-enable it.
        IS_DISABLED=$(echo "$EP_JSON" | jq -r '.status.disabled // false')
        if [ "$IS_DISABLED" = "true" ]; then
            info "Re-enabling disabled endpoint…"
            databricks postgres update-endpoint "$ENDPOINT_NAME" \
                "spec.disabled" --json '{"spec":{"disabled":false}}' \
                -o json "${PROFILE_FLAG[@]}" >/dev/null
        fi
        # Shrink to the smallest compute (0.5 CU min/max).
        info "Setting endpoint to minimum compute (min=0.5, max=0.5 CU)…"
        databricks postgres update-endpoint "$ENDPOINT_NAME" \
            "spec.autoscaling_limit_min_cu,spec.autoscaling_limit_max_cu" \
            --json '{"spec":{"autoscaling_limit_min_cu":0.5,"autoscaling_limit_max_cu":0.5}}' \
            -o json "${PROFILE_FLAG[@]}" >/dev/null 2>&1 \
            || info "  (compute update skipped — using current settings)"
        EP_JSON=$(databricks postgres get-endpoint "$ENDPOINT_NAME" \
                  -o json "${PROFILE_FLAG[@]}")
    fi

    PG_HOST=$(echo "$EP_JSON" | jq -r '.status.hosts.host')
    [ -n "$PG_HOST" ] || die "Could not read connection host from endpoint JSON:\n${EP_JSON}"
fi

# ---------------------------------------------------------------------------
# Write env vars to .env and print export statements for eval
# ---------------------------------------------------------------------------

env_set "STATSCHEMA_LAKEBASE_ENDPOINT" "$ENDPOINT_NAME"
env_set "STATSCHEMA_LAKEBASE_HOST"     "$PG_HOST"
env_set "STATSCHEMA_LAKEBASE_DB"       "$DB_NAME"
[ -n "$PG_USER" ] && env_set "STATSCHEMA_LAKEBASE_USER" "$PG_USER"

info ""
info "Lakebase endpoint ready:"
info "  STATSCHEMA_LAKEBASE_ENDPOINT = ${ENDPOINT_NAME}"
info "  STATSCHEMA_LAKEBASE_HOST     = ${PG_HOST}"
info "  STATSCHEMA_LAKEBASE_DB       = ${DB_NAME}"
[ -n "$PG_USER" ] && info "  STATSCHEMA_LAKEBASE_USER     = ${PG_USER}"
info ""
info "Saved to ${ENV_FILE}.  Run tests with:  make test-live-lakebase"

# stdout: export lines (consumed by Makefile via temp file / eval)
echo "export STATSCHEMA_LAKEBASE_ENDPOINT='${ENDPOINT_NAME}'"
echo "export STATSCHEMA_LAKEBASE_HOST='${PG_HOST}'"
echo "export STATSCHEMA_LAKEBASE_DB='${DB_NAME}'"
[ -n "$PG_USER" ] && echo "export STATSCHEMA_LAKEBASE_USER='${PG_USER}'"
