#!/usr/bin/env bash
# benchmarks/run_identity.sh
#
# Same-engine identity test: each engine is both source and target.
# Orchestrates database startup, then delegates the full matrix to
# benchmarks/run_matrix.py which runs engines in parallel and schemas
# sequentially within each engine.
#
# USAGE
#   ./benchmarks/run_identity.sh [OPTIONS]
#
# OPTIONS
#   --skip-setup        Skip container/VM startup (databases already running).
#   --skip-load         Reuse existing source schema data; skip Phase A.
#   --engines LIST      Comma-separated engines (default: postgres,cockroachdb,mysql,sqlserver,oracle,db2).
#   --schemas LIST      Comma-separated TPC schemas (default: tpcb,tpcc,tpch,tpcdi,tpcds,tpce).
#   --log-dir DIR       Directory for per-run logs (default: benchmarks/logs/identity-<ts>).
#   --phases LIST       Comma-separated pipeline phases to run (default: all).
#                       Phases: load_source,explain_source,collect_stats,load_target,
#                               explain_target,score,validate
#   --no-extended-stats Skip Layer-3 CREATE STATISTICS (faster, non-PG only anyway).
#   --schema-workers N  Override concurrent schemas per engine (default: engine-specific;
#                       postgres/cockroachdb=3, mysql=2, oracle/db2/sqlserver=1).
#                       Use 1 to force fully sequential schema execution.
#   -h, --help          Show this help.

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
LOG_DIR=""
PHASES="load_source,explain_source,collect_stats,load_target,explain_target,score,validate"
NO_EXT_STATS=""
SCHEMA_WORKERS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup)        SKIP_SETUP=1 ;;
        --skip-load)         SKIP_LOAD="--skip-load" ;;
        --engines)           ENGINES="$2"; shift ;;
        --schemas)           SCHEMAS="$2"; shift ;;
        --log-dir)           LOG_DIR="$2"; shift ;;
        --phases)            PHASES="$2"; shift ;;
        --no-extended-stats) NO_EXT_STATS="--no-extended-stats" ;;
        --schema-workers)    SCHEMA_WORKERS="--schema-workers $2"; shift ;;
        -h|--help)
            sed -n '/^# USAGE/,/^set -e/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

# ── Phase 0: start databases ─────────────────────────────────────────────────
if [[ $SKIP_SETUP -eq 0 ]]; then
    info "=== Phase 0: starting databases ==="
    start_databases "$ENGINES"
fi

# ── Phase 1-F: run matrix via Python ─────────────────────────────────────────
info "=== Identity matrix: engines=$ENGINES  schemas=$SCHEMAS ==="

# shellcheck disable=SC2086
exec "$VENV" benchmarks/run_matrix.py identity \
    --engines  "$ENGINES" \
    --schemas  "$SCHEMAS" \
    --phases   "$PHASES" \
    ${LOG_DIR:+--log-dir "$LOG_DIR"} \
    $SKIP_LOAD \
    $NO_EXT_STATS \
    $SCHEMA_WORKERS
