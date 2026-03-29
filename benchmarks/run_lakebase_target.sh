#!/usr/bin/env bash
# benchmarks/run_lakebase_target.sh
#
# Cross-database identity test with Databricks Lakebase as the target.
# 6 source engines × 6 TPC schemas = up to 36 runs.
#
# PREREQUISITES
# ─────────────
# 1. Lakebase endpoint is live.  Run `./scripts/lakebase-up.sh` once; it writes
#    STATSCHEMA_LAKEBASE_* vars to .env.
# 2. Source DB schemas are pre-loaded.  Run the identity test once per engine first:
#      python benchmarks/identity_test.py --dialect cockroachdb --dsn "$CRDB_DSN" \
#        --schema tpch --sf 0.1 --source-schema from_crdb_tpch_src
# 3. .env has DB credentials.  SQL Server password is extracted from the Lima VM log.
#
# USAGE
#   ./benchmarks/run_lakebase_target.sh [OPTIONS]
#
# OPTIONS
#   --skip-setup   Skip VM/container startup.
#   --skip-load    Reuse existing Lakebase source schemas; skip Phase A.
#   --engines LIST Comma-separated source engines (default: postgres,cockroachdb,mysql,sqlserver,oracle,db2).
#   --schemas LIST Comma-separated TPC schemas (default: tpcb,tpcc,tpch,tpcdi,tpcds,tpce).
#   --jobs N       Maximum parallel workers (default: 3).
#   --log-dir DIR  Directory for per-run logs (default: benchmarks/logs/lakebase-<ts>).
#   --phases LIST         Pipeline phases to run (default: all).
#   --full-stats-db2-ora  Collect full distribution stats on all columns for DB2/Oracle
#                         source schemas (slower; use for audit / major-release runs).
#   -h, --help            Show this help.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=benchmarks/_common.sh
source "$(dirname "$0")/_common.sh"

load_dotenv
find_python

# ── Defaults ─────────────────────────────────────────────────────────────────
SKIP_SETUP=0
SKIP_LOAD=""
ENGINES="postgres,cockroachdb,mysql,sqlserver,oracle,db2"
SCHEMAS="tpcb,tpcc,tpch,tpcdi,tpcds,tpce"
MAX_JOBS=3
LOG_DIR=""
PHASES="load_source,explain_source,collect_stats,load_target,explain_target,score,validate"
FULL_STATS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup)        SKIP_SETUP=1 ;;
        --skip-load)         SKIP_LOAD="--skip-load" ;;
        --engines)           ENGINES="$2"; shift ;;
        --schemas)           SCHEMAS="$2"; shift ;;
        --jobs)              MAX_JOBS="$2"; shift ;;
        --log-dir)           LOG_DIR="$2"; shift ;;
        --phases)            PHASES="$2"; shift ;;
        --full-stats-db2-ora) FULL_STATS="--full-stats-db2-ora" ;;
        -h|--help)
            sed -n '/^# USAGE/,/^set -e/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

# ── Lakebase credentials check ────────────────────────────────────────────────
: "${STATSCHEMA_LAKEBASE_ENDPOINT:?Must be set — run ./scripts/lakebase-up.sh}"
: "${STATSCHEMA_LAKEBASE_HOST:?Must be set — run ./scripts/lakebase-up.sh}"
export STATSCHEMA_LAKEBASE_DB="${STATSCHEMA_LAKEBASE_DB:-databricks_postgres}"

info "Lakebase endpoint : $STATSCHEMA_LAKEBASE_ENDPOINT"

# ── Phase 0: start source databases ──────────────────────────────────────────
if [[ $SKIP_SETUP -eq 0 ]]; then
    info "=== Phase 0: starting source databases ==="
    start_databases "$ENGINES"
fi

# ── Phase 1-F: run matrix via Python ─────────────────────────────────────────
info "=== Lakebase matrix: source_engines=$ENGINES  schemas=$SCHEMAS ==="

# shellcheck disable=SC2086
exec "$VENV" benchmarks/run_matrix.py lakebase \
    --source-engines "$ENGINES" \
    --schemas        "$SCHEMAS" \
    --phases         "$PHASES" \
    --max-jobs       "$MAX_JOBS" \
    ${LOG_DIR:+--log-dir "$LOG_DIR"} \
    $SKIP_LOAD \
    $FULL_STATS
