#!/usr/bin/env bash
# benchmarks/run_identity.sh
#
# Same-engine identity test: each engine is both source and target.
# Orchestrates database startup, then delegates the full matrix to
# benchmarks/run_matrix.py which runs engines in parallel and schemas
# sequentially within each engine.
#
# EXECUTION STRATEGY
#   Engines are split into two waves to avoid QEMU CPU saturation:
#
#   Wave 1 — Podman engines (postgres, cockroachdb, mysql, …):
#     Run at their native high concurrency (sw=6/4/4).  Complete in ~8 min.
#
#   Wave 2 — Lima QEMU engines (sqlserver, oracle, db2):
#     Run after Wave 1 finishes, uncontested, at sw=3 each.
#     SQL Server alone at sw=3 takes ~18 min; combined QEMU wave ~22 min.
#     Total wall time: ~22 min vs ~35 min when all engines run simultaneously.
#
#   Running all six engines together causes 2-3× QEMU slowdown because
#   the emulation threads compete with the Podman workloads for host CPUs.
#   Separating the waves eliminates that contention.
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
#   --schema-workers N  Override concurrent schemas per ALL engines (bypasses two-wave).
#   --no-wave           Disable two-wave scheduling; run all engines simultaneously.
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
NO_WAVE=0

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
        --no-wave)           NO_WAVE=1 ;;
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

# ── Split engines into Podman (fast) vs QEMU Lima (slow, emulated x86_64) ────
_QEMU_ENGINES="sqlserver oracle db2"
WAVE1_ENGINES=""   # Podman engines
WAVE2_ENGINES=""   # QEMU Lima engines
for e in ${ENGINES//,/ }; do
    if echo "$_QEMU_ENGINES" | grep -qw "$e"; then
        WAVE2_ENGINES="${WAVE2_ENGINES:+$WAVE2_ENGINES,}$e"
    else
        WAVE1_ENGINES="${WAVE1_ENGINES:+$WAVE1_ENGINES,}$e"
    fi
done

# ── Build a shared log-dir timestamp so both waves write to the same folder ──
if [[ -z "$LOG_DIR" ]]; then
    LOG_DIR="benchmarks/logs/identity-$(date +%Y%m%d-%H%M%S)"
fi

# ── Decide whether to use two-wave scheduling ────────────────────────────────
# Two-wave only makes sense when both Podman and QEMU engines are present AND
# the user hasn't pinned --schema-workers (which implies they want manual control).
USE_TWO_WAVE=0
if [[ $NO_WAVE -eq 0 && -z "$SCHEMA_WORKERS" && -n "$WAVE1_ENGINES" && -n "$WAVE2_ENGINES" ]]; then
    USE_TWO_WAVE=1
fi

# ── Phase 1-F: run matrix via Python ─────────────────────────────────────────
if [[ $USE_TWO_WAVE -eq 1 ]]; then
    info "=== Identity matrix (two-wave): wave1=$WAVE1_ENGINES  wave2=$WAVE2_ENGINES  schemas=$SCHEMAS ==="

    # Wave 1: Podman engines at their native high concurrency
    info "--- Wave 1: Podman engines ($WAVE1_ENGINES) ---"
    # shellcheck disable=SC2086
    "$VENV" benchmarks/run_matrix.py identity \
        --engines  "$WAVE1_ENGINES" \
        --schemas  "$SCHEMAS" \
        --phases   "$PHASES" \
        --log-dir  "$LOG_DIR" \
        $SKIP_LOAD \
        $NO_EXT_STATS
    wave1_rc=$?

    # Wave 2: QEMU Lima engines, uncontested, at sw=3 each
    info "--- Wave 2: QEMU engines ($WAVE2_ENGINES) with schema-workers=3 ---"
    # shellcheck disable=SC2086
    "$VENV" benchmarks/run_matrix.py identity \
        --engines       "$WAVE2_ENGINES" \
        --schemas       "$SCHEMAS" \
        --phases        "$PHASES" \
        --log-dir       "$LOG_DIR" \
        --schema-workers 3 \
        $SKIP_LOAD \
        $NO_EXT_STATS
    wave2_rc=$?

    [[ $wave1_rc -eq 0 && $wave2_rc -eq 0 ]]
else
    info "=== Identity matrix: engines=$ENGINES  schemas=$SCHEMAS ==="
    # shellcheck disable=SC2086
    exec "$VENV" benchmarks/run_matrix.py identity \
        --engines  "$ENGINES" \
        --schemas  "$SCHEMAS" \
        --phases   "$PHASES" \
        --log-dir  "$LOG_DIR" \
        $SKIP_LOAD \
        $NO_EXT_STATS \
        $SCHEMA_WORKERS
fi
