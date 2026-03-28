#!/usr/bin/env bash
# benchmarks/run_bench.sh
#
# TPC-C, TPC-H, TPC-B load benchmarks across all reachable databases.
# Delegates to benchmarks/run_matrix.py (bench mode) which calls
# run_all_bench.py and run_tpcb_bench.py.
#
# USAGE
#   ./benchmarks/run_bench.sh [OPTIONS]
#
# OPTIONS
#   --skip-setup   Skip container/VM startup.
#   --engines LIST Comma-separated engines to test (default: all).
#   --log-dir DIR  Directory for per-run logs (default: benchmarks/logs/bench-<ts>).
#   -h, --help     Show this help.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=benchmarks/_common.sh
source "$(dirname "$0")/_common.sh"

load_dotenv
find_python

# ── Defaults ─────────────────────────────────────────────────────────────────
SKIP_SETUP=0
ENGINES="postgres,cockroachdb,mysql,sqlserver,oracle,db2"
LOG_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup) SKIP_SETUP=1 ;;
        --engines)    ENGINES="$2"; shift ;;
        --log-dir)    LOG_DIR="$2"; shift ;;
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

# ── Phases 1-2: benchmarks via Python ────────────────────────────────────────
info "=== Benchmark matrix: engines=$ENGINES ==="

exec "$VENV" benchmarks/run_matrix.py bench \
    ${LOG_DIR:+--log-dir "$LOG_DIR"}
