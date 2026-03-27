#!/usr/bin/env bash
# benchmarks/run_lakebase_target.sh
#
# Runs the full cross-database → Lakebase target test matrix:
#   6 source engines × 6 TPC schemas = 36 runs
#
# PREREQUISITES
# ─────────────
# 1. Lakebase endpoint is live.  Run `./scripts/lakebase-up.sh` once; it writes
#    STATSCHEMA_LAKEBASE_* vars to .env.
# 2. Source DB schemas are pre-loaded.  Each engine needs TPC data loaded under
#    the same schema names used here (see --stats-source-schema).  The easiest
#    way is to run the identity tests once per engine first:
#      python benchmarks/identity_test.py --dialect cockroachdb --dsn "$CRDB_DSN" \
#        --schema tpch --sf 0.1 --source-schema from_crdb_tpch_src
#    That populates the engine's local schema.  Re-runs of this script use
#    --skip-load (below) so Lakebase source schemas are reused without reloading.
# 3. .env file has DB credentials (see .env.example).  SQL Server password is
#    extracted from the Lima VM log automatically.
#
# USAGE
# ─────
#   ./benchmarks/run_lakebase_target.sh [OPTIONS]
#
# OPTIONS
#   --skip-setup      Skip VM/container startup; assume everything is already up.
#   --skip-load       Pass --skip-load to identity_test.py — reuse existing
#                     Lakebase source schemas and skip Phase A data load.
#   --engines LIST    Comma-separated engines to test (default: all six).
#                     Values: postgres,cockroachdb,mysql,sqlserver,oracle,db2
#   --schemas LIST    Comma-separated schemas to test (default: all six).
#                     Values: tpcb,tpcc,tpch,tpcdi,tpcds,tpce
#   --jobs N          Maximum parallel identity_test.py processes (default: 4).
#   --log-dir DIR     Directory for per-run logs (default: benchmarks/logs/lakebase-<ts>).
#   --enrich TECH     Enrichment profile or technique to pass as --enrich (e.g. standard).
#                     Omit to use the auto-selected profile from collection_profiles.yaml.
#   -h, --help        Show this help.
#
# LEARNINGS FROM PREVIOUS RUNS
# ─────────────────────────────
# • SQL Server (Lima QEMU VM, port 14330): the mssql-server service does NOT auto-start
#   when the VM boots.  This script starts it explicitly.
#   The sa password is randomly generated at VM provision time and extracted from the
#   cloud-init log at runtime (sudo required; env var SQLSERVER_PASS is a stale fallback
#   that must be refreshed after each VM reprovision or it causes login failures).
# • Oracle XE (Lima QEMU VM, Podman container oracle-xe, port 1521): the "DATABASE IS
#   READY TO USE" log message only appears at initial container start; it is absent from
#   logs on subsequent boots.  This script fast-paths readiness via nc port check.
# • DB2 / CockroachDB / MySQL: fast-path port check before attempting container start.
# • Lakebase auth: token is generated via Databricks SDK on every connect; requires a
#   valid ~/.databrickscfg.  If you hit 403 Forbidden or TimeoutError, refresh with
#   `databricks auth login`.  Errors are transient — retry once before diagnosing.
# • --no-extended-stats is always passed: pg_restore_attribute_stats requires PG18+;
#   Lakebase uses it for injection but not all Lakebase versions expose it.
# • --dsn "" for Lakebase: identity_test.py reads from STATSCHEMA_LAKEBASE_* env vars.
# • Lakebase schema names use SHORT engine prefixes (pg18, crdb, mssql, not the full
#   dialect names postgres, cockroachdb, sqlserver).  See SCHEMA_PREFIX map in script.
# • Stats-source schemas in source DBs use the lbss_<schema> naming convention
#   (populated by identity tests run directly against each source engine).
# • --skip-load consistency: if you rebuilt the lbss_* schema in a source DB (e.g. at a
#   different scale or after a data fix), the corresponding Lakebase from_*_src schema is
#   now stale.  Running without --skip-load rebuilds the baseline from scratch;
#   --skip-load reuses the existing Lakebase schemas without reloading.
# • --no-auto-profile is the default for this script.  Enrichment techniques improve
#   stats fidelity but shift the synthetic-data distribution when Lakebase falls back to
#   ANALYZE (no direct stats injection), causing spurious within_2x failures.
#   Pass --with-enrichment to enable auto-profile selection explicitly.
# • Parallelism: run at most 4 jobs simultaneously; Lakebase connection pool is small
#   and too many parallel connections cause auth timeouts.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Load credentials from .env ──────────────────────────────────────────────
if [ -f .env ]; then
    # Export only lines that look like KEY=VALUE (skip comments and blanks).
    set -o allexport
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Z_][A-Z0-9_]*=' .env)
    set +o allexport
fi

# ── Engine name mapping ───────────────────────────────────────────────────────
# ENGINE (dialect name used on the CLI)  →  SCHEMA_PREFIX (used in schema names)
# This matches the naming convention established by the original test runs.
declare -A SCHEMA_PREFIX=(
    [postgres]="pg18"
    [cockroachdb]="crdb"
    [mysql]="mysql"
    [sqlserver]="mssql"
    [oracle]="oracle"
    [db2]="db2"
)

# ── Defaults ────────────────────────────────────────────────────────────────
SKIP_SETUP=0
SKIP_LOAD=0
ALL_ENGINES="postgres,cockroachdb,mysql,sqlserver,oracle,db2"
ALL_SCHEMAS="tpcb,tpcc,tpch,tpcdi,tpcds,tpce"
ENGINES="$ALL_ENGINES"
SCHEMAS="$ALL_SCHEMAS"
MAX_JOBS=8
LOG_DIR=""
ENRICH_FLAG=""
# --no-auto-profile is on by default for Lakebase target runs.
# Enrichment techniques improve stats fidelity but, because Lakebase falls back
# to ANALYZE on synthetic data when pg_restore_attribute_stats is unavailable,
# enriched stats shift the synthetic data distribution away from the baseline
# plan baseline — masking the true benefit and causing spurious within_2x failures.
# Pass --with-enrichment to enable auto-profile selection anyway.
NO_AUTO_PROFILE=1

# ── Parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-setup)   SKIP_SETUP=1 ;;
        --skip-load)    SKIP_LOAD=1 ;;
        --engines)      ENGINES="$2"; shift ;;
        --schemas)      SCHEMAS="$2"; shift ;;
        --jobs)         MAX_JOBS="$2"; shift ;;
        --log-dir)      LOG_DIR="$2"; shift ;;
        --enrich)            ENRICH_FLAG="--enrich $2"; NO_AUTO_PROFILE=0; shift ;;
        --with-enrichment)   NO_AUTO_PROFILE=0 ;;
        -h|--help)
            sed -n '/^# USAGE/,/^set -e/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

TS=$(date '+%Y%m%d-%H%M%S')
LOG_DIR="${LOG_DIR:-benchmarks/logs/lakebase-${TS}}"
mkdir -p "$LOG_DIR"

VENV=".venv_test/bin/python"
if [ ! -x "$VENV" ]; then
    echo "ERROR: .venv_test not found.  Run: make venv-test" >&2
    exit 1
fi

# ── Lakebase credentials check ───────────────────────────────────────────────
info()  { echo "  [$(date '+%H:%M:%S')] $*"; }
warn()  { echo "  [$(date '+%H:%M:%S')] WARN: $*" >&2; }
die()   { echo "  [$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

info "Log directory: $LOG_DIR"

: "${STATSCHEMA_LAKEBASE_ENDPOINT:?Must be set — run ./scripts/lakebase-up.sh}"
: "${STATSCHEMA_LAKEBASE_HOST:?Must be set — run ./scripts/lakebase-up.sh}"
export STATSCHEMA_LAKEBASE_DB="${STATSCHEMA_LAKEBASE_DB:-databricks_postgres}"

info "Lakebase endpoint : $STATSCHEMA_LAKEBASE_ENDPOINT"
info "Lakebase host     : $STATSCHEMA_LAKEBASE_HOST"

# ── DSN builders ─────────────────────────────────────────────────────────────
# Each function prints the DSN string for identity_test.py.
# identity_test.py parses space-separated key=value pairs.

dsn_postgres() {
    local port="${PG18_PORT:-5418}"
    # PG18 container is started with POSTGRES_PASSWORD=postgres; the separate
    # PG_PASSWORD var covers the shared pg14/pg16 test containers.
    local pass="${PG18_PASS:-${PG_PASSWORD:-postgres}}"
    echo "host=127.0.0.1 port=${port} dbname=postgres user=postgres password=${pass}"
}

dsn_cockroachdb() {
    echo "host=127.0.0.1 port=${CRDB_SINGLE_PORT:-26257} dbname=defaultdb user=${CRDB_USER:-root} sslmode=disable"
}

dsn_mysql() {
    echo "host=127.0.0.1 port=${MYSQL8_PORT:-3384} database=${MYSQL_DB:-testdb} user=root password=${MYSQL_ROOT_PASS:-testpass}"
}

dsn_sqlserver() {
    # Always try to read the authoritative password from the VM cloud-init log
    # first.  The password is randomly generated at provision time and will
    # differ after a VM reprovision, making any cached value in .env stale.
    local pass=""
    pass=$(limactl shell sqlserver22 -- \
               sudo grep "SQL Server sa password is" /var/log/cloud-init-output.log 2>/dev/null \
           | tail -1 | awk '{print $NF}' || true)
    # Fall back to the env var (e.g. for CI where there is no Lima VM).
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
    case "$1" in
        tpcb)  echo "1" ;;
        tpcc)  echo "1" ;;
        tpch)  echo "0.1" ;;
        tpcdi) echo "5" ;;
        tpcds) echo "0.01" ;;
        tpce)  echo "0.01" ;;
        *)     die "Unknown schema: $1" ;;
    esac
}

# ── Phase 0: VM / container startup ──────────────────────────────────────────

wait_port() {
    local host="$1" port="$2" label="$3" max_s="${4:-120}"
    local elapsed=0
    while ! nc -z "$host" "$port" 2>/dev/null; do
        if (( elapsed >= max_s )); then
            die "$label port $port did not open after ${max_s}s"
        fi
        sleep 5; elapsed=$(( elapsed + 5 ))
        info "  waiting for $label on $port … (${elapsed}s)"
    done
    info "$label is up on $port"
}

start_sqlserver() {
    local port="${SQLSERVER_PORT:-14330}"
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "SQL Server already accepting connections on $port"
        return
    fi
    info "Starting sqlserver22 Lima VM…"
    limactl start sqlserver22 2>&1 | grep -v '^$' || true
    # mssql-server does not auto-start on VM boot — start it explicitly.
    info "Starting mssql-server service inside VM…"
    limactl shell sqlserver22 -- sudo systemctl start mssql-server 2>/dev/null || true
    sleep 10
    wait_port 127.0.0.1 "$port" "SQL Server" 60
}

start_oracle() {
    local port="${ORACLE_PORT:-1521}"
    # Fast path: Oracle port already open → container is ready; no wait needed.
    # The "DATABASE IS READY TO USE" log message only appears at initial boot,
    # so log-watching breaks when the container has been running for a while.
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "Oracle XE already accepting connections on $port"
        return
    fi
    info "Starting oracle Lima VM…"
    limactl start oracle 2>&1 | grep -v '^$' || true
    # Wait for the container to announce readiness (reliable on first boot).
    info "Waiting for Oracle XE to signal readiness…"
    local elapsed=0 max_s=300
    until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
          | grep -q "DATABASE IS READY TO USE" \
          || nc -z 127.0.0.1 "$port" 2>/dev/null; do
        if (( elapsed >= max_s )); then
            die "Oracle XE did not become ready after ${max_s}s"
        fi
        sleep 15; elapsed=$(( elapsed + 15 ))
        info "  waiting for Oracle XE… (${elapsed}s)"
    done
    wait_port 127.0.0.1 "$port" "Oracle XE" 30
}

start_db2() {
    local port="${DB2_PORT:-50000}"
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "DB2 already accepting connections on $port"
        return
    fi
    info "Starting db2 Lima VM…"
    limactl start db2 2>&1 | grep -v '^$' || true
    # DB2 starts its instance service automatically, but it takes time.
    wait_port 127.0.0.1 "$port" "DB2" 180
}

start_cockroachdb() {
    local port="${CRDB_SINGLE_PORT:-26257}"
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "CockroachDB already up on $port"
        return
    fi
    info "Starting CockroachDB container…"
    podman start crdb-single 2>/dev/null \
        || podman run -d --name crdb-single \
               -p "${port}:${port}" \
               cockroachdb/cockroach:latest \
               start-single-node --insecure 2>/dev/null \
        || true
    wait_port 127.0.0.1 "$port" "CockroachDB" 60
}

start_mysql() {
    local port="${MYSQL8_PORT:-3384}"
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "MySQL already up on $port"
        return
    fi
    info "Starting MySQL 8 container…"
    podman start mysql8 2>/dev/null \
        || podman run -d --name mysql8 \
               -e MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASS:-testpass}" \
               -p "${port}:3306" \
               mysql:8 2>/dev/null \
        || true
    wait_port 127.0.0.1 "$port" "MySQL 8" 60
}

start_postgres() {
    local port="${PG18_PORT:-5418}"
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        info "PostgreSQL 18 already up on $port"
        return
    fi
    info "Starting PostgreSQL 18 container…"
    podman start pg18 2>/dev/null \
        || podman run -d --name pg18 \
               -e POSTGRES_PASSWORD="${PG_PASSWORD:-testpass}" \
               -p "${port}:5432" \
               postgres:18 2>/dev/null \
        || true
    wait_port 127.0.0.1 "$port" "PostgreSQL 18" 60
}

if [[ $SKIP_SETUP -eq 0 ]]; then
    info "=== Phase 0: starting source databases ==="
    # Lima VMs are started sequentially (QEMU emulation is CPU-heavy; avoid
    # starting all three simultaneously on Apple Silicon).
    IFS=',' read -ra ENG_LIST <<< "$ENGINES"
    NEED_SS=0; NEED_ORACLE=0; NEED_DB2=0
    for e in "${ENG_LIST[@]}"; do
        case "$e" in
            sqlserver) NEED_SS=1 ;;
            oracle)    NEED_ORACLE=1 ;;
            db2)       NEED_DB2=1 ;;
        esac
    done
    [[ $NEED_SS     -eq 1 ]] && start_sqlserver
    [[ $NEED_ORACLE -eq 1 ]] && start_oracle
    [[ $NEED_DB2    -eq 1 ]] && start_db2

    # Podman containers are fast; start in the background.
    for e in "${ENG_LIST[@]}"; do
        case "$e" in
            cockroachdb) start_cockroachdb & ;;
            mysql)       start_mysql & ;;
            postgres)    start_postgres & ;;
        esac
    done
    wait
    info "All source databases are up."
fi

# ── Phase 1: run test matrix ──────────────────────────────────────────────────

PASS=0; FAIL=0; SKIP=0
RESULTS_FILE="$LOG_DIR/summary.txt"

# Semaphore-style job limiter using a named pipe.
SEMAPHORE=$(mktemp)
rm -f "$SEMAPHORE"
mkfifo "$SEMAPHORE"
exec 9<>"$SEMAPHORE"
# Seed the semaphore with MAX_JOBS tokens.
for _ in $(seq 1 "$MAX_JOBS"); do echo >&9; done

declare -A PID_TO_LABEL

run_one() {
    local engine="$1" schema="$2"
    local sf prefix src_dsn
    sf=$(sf_for "$schema")
    prefix="${SCHEMA_PREFIX[$engine]}"

    # Schema names in Lakebase use the short prefix (pg18, crdb, mssql, etc.);
    # the stats-source schema in the source DB uses the lbss_* convention.
    local src_schema="from_${prefix}_${schema}_src"
    local tgt_schema="from_${prefix}_${schema}_tgt"
    local ss_schema="lbss_${schema}"
    local log_file="$LOG_DIR/${engine}_${schema}.log"
    local label="${engine}×${schema}(sf=${sf})"

    # Build engine DSN.
    case "$engine" in
        postgres)    src_dsn=$(dsn_postgres) ;;
        cockroachdb) src_dsn=$(dsn_cockroachdb) ;;
        mysql)       src_dsn=$(dsn_mysql) ;;
        sqlserver)   src_dsn=$(dsn_sqlserver) ;;
        oracle)      src_dsn=$(dsn_oracle) ;;
        db2)         src_dsn=$(dsn_db2) ;;
        *) warn "Unknown engine $engine — skipping"; return ;;
    esac

    local skip_load_flag=""
    [[ $SKIP_LOAD -eq 1 ]] && skip_load_flag="--skip-load"

    local no_auto_profile_flag=""
    [[ $NO_AUTO_PROFILE -eq 1 ]] && no_auto_profile_flag="--no-auto-profile"

    # Acquire a semaphore token (blocks when MAX_JOBS are already running).
    read -r -u 9

    {
        info "START $label"
        local exit_code=0
        # shellcheck disable=SC2086
        "$VENV" benchmarks/identity_test.py \
            --schema    "$schema" \
            --sf        "$sf" \
            --dialect   lakebase \
            --dsn       "" \
            --source-schema         "$src_schema" \
            --target-schema         "$tgt_schema" \
            --stats-source-dsn      "$src_dsn" \
            --stats-source-dialect  "$engine" \
            --stats-source-schema   "$ss_schema" \
            --no-extended-stats \
            $skip_load_flag \
            $no_auto_profile_flag \
            $ENRICH_FLAG \
            >"$log_file" 2>&1 || exit_code=$?

        if [[ $exit_code -eq 0 ]]; then
            # Extract key metrics from the last PASS/FAIL line.
            local score
            score=$(grep -E "PASS|FAIL" "$log_file" | tail -1 || echo "(no score)")
            printf "PASS  %-40s  %s\n" "$label" "$score" | tee -a "$RESULTS_FILE"
        else
            printf "FAIL  %-40s  exit=%d\n" "$label" "$exit_code" | tee -a "$RESULTS_FILE"
            # Tail the log so failures are visible in the parent terminal.
            echo "      --- last 10 lines of $log_file ---" >&2
            tail -10 "$log_file" >&2
            echo "      ---" >&2
        fi

        # Release the semaphore token.
        echo >&9
    } &

    PID_TO_LABEL[$!]="$label"
}

info "=== Phase 1: running test matrix (engines=$ENGINES, schemas=$SCHEMAS, jobs=$MAX_JOBS) ==="
echo "# $(date)" > "$RESULTS_FILE"
echo "# engines=$ENGINES  schemas=$SCHEMAS  skip_load=$SKIP_LOAD  enrich=${ENRICH_FLAG:-auto}" >> "$RESULTS_FILE"
echo "" >> "$RESULTS_FILE"

IFS=',' read -ra ENG_LIST   <<< "$ENGINES"
IFS=',' read -ra SCH_LIST   <<< "$SCHEMAS"

for engine in "${ENG_LIST[@]}"; do
    for schema in "${SCH_LIST[@]}"; do
        run_one "$engine" "$schema"
    done
done

# Wait for all background jobs to finish.
wait

# ── Phase 2: print summary ────────────────────────────────────────────────────
info "=== Results ==="
cat "$RESULTS_FILE"

PASS=$(grep -c "^PASS" "$RESULTS_FILE" 2>/dev/null || true)
FAIL=$(grep -c "^FAIL" "$RESULTS_FILE" 2>/dev/null || true)
TOTAL=$(( PASS + FAIL ))

echo ""
echo "  Passed : $PASS / $TOTAL"
echo "  Failed : $FAIL / $TOTAL"
echo "  Logs   : $LOG_DIR"
echo ""

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
