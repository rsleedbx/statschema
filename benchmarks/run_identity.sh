#!/usr/bin/env bash
# benchmarks/run_identity.sh
#
# Runs pure identity tests: each engine is both source and target.
# Same engine → same engine, two schemas on the same database.
#
#   A load source → B baseline EXPLAIN → C collect stats →
#   D build copy + inject → E replay EXPLAIN → F score
#
# PARALLELISM MODEL
# ─────────────────
# One background worker is spawned per engine — engines run in parallel.
# Within each worker the TPC schemas run sequentially, so a single-node
# engine (CockroachDB, MySQL, SQL Server, Oracle, DB2) is never hit by
# more than one heavy COPY operation at a time.
#
#   postgres_worker   tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┐
#   cockroachdb_worker tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┤ all
#   mysql_worker      tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┤ in
#   sqlserver_worker  tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┤ parallel
#   oracle_worker     tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┤
#   db2_worker        tpcb → tpcc → tpch → tpcdi → tpcds → tpce  ─┘
#
# LOG FILES  (per run, in LOG_DIR)
# ────────────────────────────────
#   <engine>_<schema>.out   — stdout of identity_test.py (progress, scores)
#   <engine>_<schema>.err   — stderr of identity_test.py (warnings, tracebacks)
#   summary.txt             — one PASS/FAIL line per run, written at end
#
# PREREQUISITES
# ─────────────
# 1. Source databases are running.  The script starts containers automatically
#    unless --skip-setup is passed.  Lima VMs (SQL Server, Oracle, DB2) must
#    be provisioned separately — see docs/setup/*.
# 2. .env file has DB credentials.  Copy .env.example and fill in passwords.
#
# USAGE
#   ./benchmarks/run_identity.sh [OPTIONS]
#
# OPTIONS
#   --skip-setup      Skip container/VM startup; assume databases are already up.
#   --skip-load       Pass --skip-load to identity_test.py — reuse existing
#                     source schema data and skip Phase A.
#   --engines LIST    Comma-separated engines to run.
#                     Default: postgres,cockroachdb,mysql,sqlserver,oracle,db2
#   --schemas LIST    Comma-separated TPC schemas to run.
#                     Default: tpcb,tpcc,tpch,tpcdi,tpcds,tpce
#   --log-dir DIR     Directory for per-run logs (default: benchmarks/logs/identity-<ts>).
#   --no-extended-stats
#                     Pass --no-extended-stats to every run (Layer 2 only, faster).
#                     Non-PG engines (mysql, sqlserver, oracle, db2) always use this.
#   -h, --help        Show this help.
#
# SCALE FACTORS  (per engine × schema)
# ──────────────────────────────────────────────────────────────────────
#   Engine         tpcb  tpcc  tpch   tpcdi  tpcds   tpce
#   ────────────────────────────────────────────────────────────────────
#   postgres/crdb     1     1   0.1       5   0.01   0.01
#   mysql / db2       1     1  0.01       1   0.01   0.01
#   sqlserver/oracle  1     1  0.01       1   0.01   0.10
#
# KNOWN RESULTS (from identity_results.md)
# ─────────────────────────────────────────
# All engines pass all schemas except SQL Server × TPC-DI (within_2x=0.44
# < 0.50 threshold — known statistics injection fidelity issue).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Load credentials from .env ───────────────────────────────────────────────
if [ -f .env ]; then
    set -o allexport
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Z_][A-Z0-9_]*=' .env)
    set +o allexport
fi

# ── Defaults ─────────────────────────────────────────────────────────────────
SKIP_SETUP=0
SKIP_LOAD=0
ENGINES="postgres,cockroachdb,mysql,sqlserver,oracle,db2"
SCHEMAS="tpcb,tpcc,tpch,tpcdi,tpcds,tpce"
LOG_DIR=""
NO_EXT_STATS_FLAG=""

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup)        SKIP_SETUP=1 ;;
        --skip-load)         SKIP_LOAD=1 ;;
        --engines)           ENGINES="$2"; shift ;;
        --schemas)           SCHEMAS="$2"; shift ;;
        --log-dir)           LOG_DIR="$2"; shift ;;
        --no-extended-stats) NO_EXT_STATS_FLAG="--no-extended-stats" ;;
        -h|--help)
            sed -n '/^# USAGE/,/^set -e/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

TS=$(date '+%Y%m%d-%H%M%S')
LOG_DIR="${LOG_DIR:-benchmarks/logs/identity-${TS}}"
mkdir -p "$LOG_DIR"

VENV=".venv_test/bin/python"
if [ ! -x "$VENV" ]; then
    echo "ERROR: .venv_test not found.  Run: make venv-test" >&2
    exit 1
fi

info() { echo "  [$(date '+%H:%M:%S')] $*"; }
warn() { echo "  [$(date '+%H:%M:%S')] WARN: $*" >&2; }
die()  { echo "  [$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

info "Log directory: $LOG_DIR"

# ── DSN builders ─────────────────────────────────────────────────────────────

dsn_postgres() {
    echo "host=127.0.0.1 port=${PG18_PORT:-5418} dbname=postgres user=postgres password=${PG18_PASS:-${PG_PASSWORD:-postgres}}"
}

dsn_cockroachdb() {
    echo "host=127.0.0.1 port=${CRDB_SINGLE_PORT:-26257} dbname=defaultdb user=${CRDB_USER:-root} sslmode=disable"
}

dsn_mysql() {
    echo "host=127.0.0.1 port=${MYSQL8_PORT:-3384} database=${MYSQL_DB:-testdb} user=root password=${MYSQL_ROOT_PASS:-testpass}"
}

dsn_sqlserver() {
    local pass=""
    pass=$(limactl shell sqlserver22 -- \
               sudo grep "SQL Server sa password is" /var/log/cloud-init-output.log 2>/dev/null \
           | tail -1 | awk '{print $NF}' || true)
    pass="${pass:-${SQLSERVER_PASS:-}}"
    [ -n "$pass" ] || die "SQL Server password unknown.  Set SQLSERVER_PASS in .env or start the sqlserver22 Lima VM."
    echo "server=127.0.0.1 port=${SQLSERVER_PORT:-14330} database=master user=sa password=${pass}"
}

dsn_oracle() {
    echo "host=${ORACLE_HOST:-127.0.0.1} port=${ORACLE_PORT:-1521} service=${ORACLE_SERVICE:-XE} user=${ORACLE_USER:-system} password=${ORACLE_PASS:-oracle}"
}

dsn_db2() {
    echo "host=${DB2_HOST:-127.0.0.1} port=${DB2_PORT:-50000} database=${DB2_DATABASE:-testdb} user=${DB2_USER:-db2inst1} password=${DB2_PASS:-testpass}"
}

# ── Scale factors ─────────────────────────────────────────────────────────────

sf_for() {
    local engine="$1" schema="$2"
    case "$engine" in
        postgres|cockroachdb|neon|lakebase)
            case "$schema" in
                tpcb)  echo "1" ;;    tpcc)  echo "1" ;;
                tpch)  echo "0.1" ;;  tpcdi) echo "5" ;;
                tpcds) echo "0.01" ;; tpce)  echo "0.01" ;;
                *) die "Unknown schema: $schema" ;;
            esac ;;
        mysql|db2)
            case "$schema" in
                tpcb)  echo "1" ;;    tpcc)  echo "1" ;;
                tpch)  echo "0.01" ;; tpcdi) echo "1" ;;
                tpcds) echo "0.01" ;; tpce)  echo "0.01" ;;
                *) die "Unknown schema: $schema" ;;
            esac ;;
        sqlserver|oracle)
            case "$schema" in
                tpcb)  echo "1" ;;    tpcc)  echo "1" ;;
                tpch)  echo "0.01" ;; tpcdi) echo "1" ;;
                tpcds) echo "0.01" ;; tpce)  echo "0.1" ;;
                *) die "Unknown schema: $schema" ;;
            esac ;;
        *)
            case "$schema" in
                tpcb)  echo "1" ;;    tpcc)  echo "1" ;;
                tpch)  echo "0.1" ;;  tpcdi) echo "5" ;;
                tpcds) echo "0.01" ;; tpce)  echo "0.01" ;;
                *) die "Unknown schema: $schema" ;;
            esac ;;
    esac
}

# ── Container / VM startup helpers ───────────────────────────────────────────

wait_port() {
    local host="$1" port="$2" label="$3" max_s="${4:-120}"
    local elapsed=0
    while ! nc -z "$host" "$port" 2>/dev/null; do
        (( elapsed >= max_s )) && die "$label port $port did not open after ${max_s}s"
        sleep 5; elapsed=$(( elapsed + 5 ))
        info "  waiting for $label on $port … (${elapsed}s)"
    done
    info "$label is up on $port"
}

start_postgres() {
    local port="${PG18_PORT:-5418}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "PostgreSQL 18 already up on $port"; return; }
    info "Starting PostgreSQL 18 container…"
    podman start pg18 2>/dev/null \
        || podman run -d --name pg18 \
               -e POSTGRES_PASSWORD="${PG18_PASS:-${PG_PASSWORD:-postgres}}" \
               -p "${port}:5432" postgres:18 2>/dev/null || true
    wait_port 127.0.0.1 "$port" "PostgreSQL 18" 60
}

start_cockroachdb() {
    local port="${CRDB_SINGLE_PORT:-26257}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "CockroachDB already up on $port"; return; }
    info "Starting CockroachDB container…"
    podman start crdb-single 2>/dev/null \
        || podman run -d --name crdb-single \
               -p "${port}:${port}" cockroachdb/cockroach:latest \
               start-single-node --insecure 2>/dev/null || true
    wait_port 127.0.0.1 "$port" "CockroachDB" 60
}

start_mysql() {
    local port="${MYSQL8_PORT:-3384}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "MySQL 8 already up on $port"; return; }
    info "Starting MySQL 8 container…"
    podman start mysql8 2>/dev/null \
        || podman run -d --name mysql8 \
               -e MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASS:-testpass}" \
               -p "${port}:3306" mysql:8 2>/dev/null || true
    wait_port 127.0.0.1 "$port" "MySQL 8" 60
}

start_sqlserver() {
    local port="${SQLSERVER_PORT:-14330}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "SQL Server already up on $port"; return; }
    info "Starting sqlserver22 Lima VM…"
    limactl start sqlserver22 2>&1 | grep -v '^$' || true
    limactl shell sqlserver22 -- sudo systemctl start mssql-server 2>/dev/null || true
    sleep 10
    wait_port 127.0.0.1 "$port" "SQL Server" 60
}

start_oracle() {
    local port="${ORACLE_PORT:-1521}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "Oracle XE already up on $port"; return; }
    info "Starting oracle Lima VM…"
    limactl start oracle 2>&1 | grep -v '^$' || true
    local elapsed=0 max_s=300
    until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
          | grep -q "DATABASE IS READY TO USE" \
          || nc -z 127.0.0.1 "$port" 2>/dev/null; do
        (( elapsed >= max_s )) && die "Oracle XE did not become ready after ${max_s}s"
        sleep 15; elapsed=$(( elapsed + 15 ))
        info "  waiting for Oracle XE… (${elapsed}s)"
    done
    wait_port 127.0.0.1 "$port" "Oracle XE" 30
}

start_db2() {
    local port="${DB2_PORT:-50000}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "DB2 already up on $port"; return; }
    info "Starting db2 Lima VM…"
    limactl start db2 2>&1 | grep -v '^$' || true
    wait_port 127.0.0.1 "$port" "DB2" 180
}

# ── Phase 0: start databases ─────────────────────────────────────────────────

if [[ $SKIP_SETUP -eq 0 ]]; then
    info "=== Phase 0: starting databases ==="
    IFS=',' read -ra ENG_LIST <<< "$ENGINES"
    NEED_SS=0; NEED_ORACLE=0; NEED_DB2=0
    for e in "${ENG_LIST[@]}"; do
        case "$e" in sqlserver) NEED_SS=1 ;; oracle) NEED_ORACLE=1 ;; db2) NEED_DB2=1 ;; esac
    done
    # Lima VMs sequentially (QEMU is CPU-heavy); containers in background
    [[ $NEED_SS     -eq 1 ]] && start_sqlserver
    [[ $NEED_ORACLE -eq 1 ]] && start_oracle
    [[ $NEED_DB2    -eq 1 ]] && start_db2
    for e in "${ENG_LIST[@]}"; do
        case "$e" in
            postgres)    start_postgres    & ;;
            cockroachdb) start_cockroachdb & ;;
            mysql)       start_mysql       & ;;
        esac
    done
    wait
    info "All databases are up."
fi

# ── Phase 1: run identity test matrix ────────────────────────────────────────
#
# Design: one background worker per engine (engines parallel).
# Within each worker: schemas run sequentially (prevents overloading single-node
# engines with concurrent heavy COPY operations).
#
# Log files per run:
#   <engine>_<schema>.out   stdout (progress lines, scores, result path)
#   <engine>_<schema>.err   stderr (Python warnings, tracebacks)
#
# Each engine writes to its own partial-summary file to avoid concurrent writes
# to the shared summary.txt.  They are merged at the end.

IFS=',' read -ra ENG_LIST <<< "$ENGINES"
IFS=',' read -ra SCH_LIST <<< "$SCHEMAS"

RESULTS_FILE="$LOG_DIR/summary.txt"

# run_one ENGINE SCHEMA — run identity_test.py, print result line to engine
# partial-summary.  Called sequentially within an engine worker.
run_one() {
    local engine="$1" schema="$2"
    local sf dsn ext_stats_flag="" db2_env="" skip_load_flag="" exit_code=0

    sf=$(sf_for "$engine" "$schema")

    case "$engine" in
        postgres)    dsn=$(dsn_postgres)    ;;
        cockroachdb) dsn=$(dsn_cockroachdb) ;;
        mysql)       dsn=$(dsn_mysql)       ;;
        sqlserver)   dsn=$(dsn_sqlserver)   ;;
        oracle)      dsn=$(dsn_oracle)      ;;
        db2)         dsn=$(dsn_db2)         ;;
        *) warn "Unknown engine $engine — skipping"; return ;;
    esac

    # Non-PG engines: always disable extended stats (different CREATE STATISTICS
    # syntax; CockroachDB / MySQL / SQL Server / Oracle / DB2 not supported).
    ext_stats_flag="$NO_EXT_STATS_FLAG"
    case "$engine" in
        mysql|sqlserver|oracle|db2) ext_stats_flag="--no-extended-stats" ;;
    esac

    [[ "$engine" == "db2" && -n "${DB2_CONTAINER_NAME:-}" ]] \
        && db2_env="DB2_CONTAINER_NAME=$DB2_CONTAINER_NAME"

    [[ $SKIP_LOAD -eq 1 ]] && skip_load_flag="--skip-load"

    local out_file="$LOG_DIR/${engine}_${schema}.out"
    local err_file="$LOG_DIR/${engine}_${schema}.err"
    local label="${engine}×${schema}(sf=${sf})"

    info "START  $label"
    # shellcheck disable=SC2086
    env $db2_env "$VENV" benchmarks/identity_test.py \
        --schema  "$schema" \
        --sf      "$sf" \
        --dialect "$engine" \
        --dsn     "$dsn" \
        $ext_stats_flag \
        $skip_load_flag \
        >"$out_file" 2>"$err_file" || exit_code=$?

    local eng_summary="$LOG_DIR/_${engine}.partial"

    if [[ $exit_code -eq 0 ]]; then
        local score
        score=$(grep -E "Overall:" "$out_file" | tail -1 || echo "(no score line)")
        printf "PASS  %-42s  %s\n" "$label" "$score" >> "$eng_summary"
        info "PASS   $label  —  $score"
    else
        printf "FAIL  %-42s  exit=%d\n" "$label" "$exit_code" >> "$eng_summary"
        info "FAIL   $label  (exit=$exit_code) — see $err_file"
        # Print last 5 lines of stderr inline so the user sees the error
        # without having to open the file
        if [[ -s "$err_file" ]]; then
            echo "       ┌── last 5 lines of ${err_file##*/} ──" >&2
            tail -5 "$err_file" | sed 's/^/       │ /' >&2
            echo "       └────────────────────────────────────" >&2
        fi
    fi
}

# engine_worker ENGINE — run all schemas for this engine sequentially.
engine_worker() {
    local engine="$1"
    local eng_summary="$LOG_DIR/_${engine}.partial"
    : > "$eng_summary"   # create / clear partial file

    info "WORKER $engine starting (${#SCH_LIST[@]} schemas sequential)"
    for schema in "${SCH_LIST[@]}"; do
        run_one "$engine" "$schema"
    done
    info "WORKER $engine done"
}

info "=== Phase 1: identity matrix — engines in parallel, schemas sequential ==="
info "    engines : $ENGINES"
info "    schemas : $SCHEMAS"
info "    log dir : $LOG_DIR"

# Launch one worker per engine in the background
for engine in "${ENG_LIST[@]}"; do
    engine_worker "$engine" &
done

# Wait for all workers to finish
wait

# ── Phase 2: assemble summary and print results ───────────────────────────────

{
    echo "# $(date)"
    echo "# engines=$ENGINES"
    echo "# schemas=$SCHEMAS"
    echo "# skip_load=$SKIP_LOAD"
    echo ""
    for engine in "${ENG_LIST[@]}"; do
        partial="$LOG_DIR/_${engine}.partial"
        [[ -f "$partial" ]] && cat "$partial"
    done
} > "$RESULTS_FILE"

info "=== Results ==="
cat "$RESULTS_FILE"

PASS=$(grep -c "^PASS" "$RESULTS_FILE" 2>/dev/null || true)
FAIL=$(grep -c "^FAIL" "$RESULTS_FILE" 2>/dev/null || true)
TOTAL=$(( PASS + FAIL ))

echo ""
echo "  Passed : $PASS / $TOTAL"
echo "  Failed : $FAIL / $TOTAL"
echo ""
echo "  Logs   : $LOG_DIR"
echo "           <engine>_<schema>.out  — stdout (progress, scores)"
echo "           <engine>_<schema>.err  — stderr (warnings, tracebacks)"
echo "  Results: benchmarks/results/"
echo ""
echo "  Expected failures: SQL Server × TPC-DI (within_2x=0.44 — known limit)"
echo ""

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
