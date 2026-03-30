# benchmarks/_common.sh
#
# Shared shell library sourced by all run_*.sh scripts.
# NOT executable directly — source it: source "$(dirname "$0")/_common.sh"
#
# Provides:
#   load_dotenv          — load .env from repo root (safe grep-based approach)
#   find_python          — sets VENV to the virtualenv Python; dies if missing
#   info / warn / die    — timestamped log helpers
#   wait_port            — wait until a TCP port is open (necessary but not sufficient)
#   wait_db              — wait until the engine accepts a real connection (use this)
#   start_postgres       — start PG18 container if not up
#   start_cockroachdb    — start CockroachDB container if not up
#   start_mysql          — start MySQL 8 container if not up
#   start_sqlserver      — start sqlserver22 Lima VM if not up
#   start_oracle         — start oracle Lima VM / oracle-xe container if not up
#   start_db2            — start db2 Lima VM if not up
#   start_databases      — Phase 0 orchestrator (Lima sequential, containers parallel)
#   dsn_postgres/cockroachdb/mysql/sqlserver/oracle/db2
#                        — print the DSN string for identity_test.py
#   export_bench_sqlserver_dsn
#                        — set BENCH_SQLSERVER_DSN for Python bench scripts
#   sf_for ENGINE SCHEMA — print the scale factor for an engine × schema pair

# ── .env loading ──────────────────────────────────────────────────────────────
# Exports only lines matching KEY=VALUE (skips comments, blank lines, lowercase).
# Called once at the top of each run_*.sh after REPO_ROOT is set.
load_dotenv() {
    local env_file="${REPO_ROOT:-.}/.env"
    if [[ -f "$env_file" ]]; then
        set -o allexport
        # shellcheck disable=SC1090
        source <(grep -E '^[A-Z_][A-Z0-9_]*=' "$env_file")
        set +o allexport
    fi
}

# ── Python virtualenv ─────────────────────────────────────────────────────────
# Sets VENV to .venv_test/bin/python (relative to REPO_ROOT).
# Dies with a helpful message if the venv does not exist.
find_python() {
    VENV="${REPO_ROOT}/.venv_test/bin/python"
    if [[ ! -x "$VENV" ]]; then
        echo "ERROR: .venv_test not found.  Run: make venv-test" >&2
        exit 1
    fi
}

# ── Log helpers ───────────────────────────────────────────────────────────────
info() { echo "  [$(date '+%H:%M:%S')] $*"; }
warn() { echo "  [$(date '+%H:%M:%S')] WARN: $*" >&2; }
die()  { echo "  [$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

# ── wait_port HOST PORT LABEL [MAX_SECONDS=120] ───────────────────────────────
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

# ── wait_db DSN_SNIPPET ENGINE LABEL [MAX_SECONDS=120] ────────────────────────
# Polls until a real database connection succeeds — not just TCP reachability.
# DSN_SNIPPET is passed to bench_config.py via the ENGINE argument.
# This catches the "port open but database not accepting connections yet" race.
wait_db() {
    local engine="$1" label="$2" max_s="${3:-120}"
    local elapsed=0
    until "$VENV" benchmarks/bench_config.py ping "$engine" 2>/dev/null; do
        (( elapsed >= max_s )) && die "$label did not accept connections after ${max_s}s"
        sleep 10; elapsed=$(( elapsed + 10 ))
        info "  waiting for $label to accept connections… (${elapsed}s)"
    done
    info "$label is accepting connections"
}

# ── Container / VM startup ────────────────────────────────────────────────────

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
    if "$VENV" benchmarks/bench_config.py ping sqlserver 2>/dev/null; then
        info "SQL Server already accepting connections on $port"; return
    fi
    info "Starting sqlserver22 Lima VM…"
    limactl start sqlserver22 2>&1 | grep -v '^$' || true
    # mssql-server does not auto-start on VM boot — start it explicitly.
    limactl shell sqlserver22 -- sudo systemctl start mssql-server 2>/dev/null || true
    wait_port 127.0.0.1 "$port" "SQL Server" 90
    wait_db sqlserver "SQL Server" 120
}

start_oracle() {
    local port="${ORACLE_PORT:-1521}"
    nc -z 127.0.0.1 "$port" 2>/dev/null && { info "Oracle XE already up on $port"; return; }
    info "Starting oracle Lima VM…"
    limactl start oracle 2>&1 | grep -v '^$' || true
    # "DATABASE IS READY TO USE" only appears at initial boot; fall back to port check.
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
    # TCP port open is insufficient — Lima's port-forward is active even while the
    # DB2 container runs its 300-second first-boot Task #3 (db2iupdt).  A TCP
    # connection succeeds but immediately resets (SQL30081N ECONNRESET) until
    # db2start completes.  Use a real connection probe instead.
    if "$VENV" benchmarks/bench_config.py ping db2 2>/dev/null; then
        info "DB2 already accepting connections on $port"; return
    fi
    info "Starting db2 Lima VM…"
    limactl start db2 2>&1 | grep -v '^$' || true
    wait_port 127.0.0.1 "$port" "DB2 (TCP)" 180
    # Now wait for the DB2 instance to fully start (db2iupdt Task #3 ~ 300s).
    info "Waiting for DB2 instance to accept connections (first-boot init may take ~5 min)…"
    wait_db db2 "DB2" 600
}

# ── bench_prereq_check [ENGINE] ───────────────────────────────────────────────
# Non-interactive baseline check sourced at the top of run_*.sh scripts.
# Prints a CPU snapshot and warns (but does not abort) when:
#   - active benchmark processes are detected,
#   - QEMU VMs not needed for ENGINE are still running and consuming CPU, or
#   - host load average is at or above the physical core count.
#
# This function never kills anything — it only reports and optionally suggests
# running bench_baseline.sh.  Call it before start_databases.
#
# ENGINE  all (default) | sqlserver | oracle | db2 | podman
bench_prereq_check() {
    local engine="${1:-all}"
    local cores load15 qemu_count warn_issued=0

    cores=$(sysctl -n hw.physicalcpu 2>/dev/null || echo "?")
    load15=$(uptime | awk -F'load averages:|load average:' '{print $2}' | awk '{print $NF}' | tr -d ',')

    info "── Prereq check (engine=${engine}) ──"

    # Active benchmark processes
    local bench_pids
    bench_pids=$(pgrep -f "run_matrix\.py|identity_test\.py|run_bench\.py|run_identity\.sh" 2>/dev/null || true)
    if [[ -n "$bench_pids" ]]; then
        warn "Active benchmark processes detected (may skew results):"
        pgrep -la "run_matrix\.py|identity_test\.py|run_bench\.py|run_identity\.sh" 2>/dev/null \
            | sed 's/^/    /' || true
        warn "Run: benchmarks/bench_baseline.sh --mode=wait  to block until they finish."
        warn_issued=1
    fi

    # QEMU VMs not needed for this engine
    declare -A needed_vms=()
    case "$engine" in
        sqlserver) needed_vms[sqlserver22]=1 ;;
        oracle)    needed_vms[oracle]=1      ;;
        db2)       needed_vms[db2]=1         ;;
        all)       needed_vms[sqlserver22]=1; needed_vms[oracle]=1; needed_vms[db2]=1 ;;
    esac

    for vm in sqlserver22 oracle db2; do
        local is_running
        is_running=$(limactl list 2>/dev/null | awk -v v="$vm" '$1==v && $2=="Running" {print 1}')
        if [[ -n "$is_running" && -z "${needed_vms[$vm]+x}" ]]; then
            warn "$vm is Running but not needed for engine=${engine} — consuming host QEMU CPU."
            warn "Run: benchmarks/bench_baseline.sh --mode=isolate --engine=${engine}"
            warn_issued=1
        fi
    done

    # Load average
    qemu_count=$(ps -eo comm 2>/dev/null | grep -c "qemu-system-x86_64" || true)
    if [[ "$cores" != "?" ]] && ! awk -v l="$load15" -v c="$cores" 'BEGIN{exit !(l < c)}' 2>/dev/null; then
        warn "Load average ${load15} >= ${cores} cores — host may be saturated."
        warn_issued=1
    fi

    if [[ $warn_issued -eq 0 ]]; then
        info "Host is clean.  ${qemu_count} QEMU thread(s) running.  Load: ${load15}/${cores}."
    fi
    info "────────────────────────────────────────────────────"
}

# start_databases ENGINE_COMMA_LIST
# Starts only the databases present in the comma-separated list.
# Lima VMs (sqlserver, oracle, db2) are started sequentially because QEMU
# emulation is CPU-heavy on Apple Silicon.  Podman containers start in parallel.
start_databases() {
    local engines="$1"
    local -a eng_list
    IFS=',' read -ra eng_list <<< "$engines"

    local need_ss=0 need_oracle=0 need_db2=0
    for e in "${eng_list[@]}"; do
        case "$e" in
            sqlserver) need_ss=1     ;;
            oracle)    need_oracle=1 ;;
            db2)       need_db2=1    ;;
        esac
    done

    [[ $need_ss     -eq 1 ]] && start_sqlserver
    [[ $need_oracle -eq 1 ]] && start_oracle
    [[ $need_db2    -eq 1 ]] && start_db2

    for e in "${eng_list[@]}"; do
        case "$e" in
            postgres)    start_postgres    & ;;
            cockroachdb) start_cockroachdb & ;;
            mysql)       start_mysql       & ;;
        esac
    done
    wait
    info "All databases are up."
}

# ── DSN builders ──────────────────────────────────────────────────────────────
# Delegates to benchmarks/bench_config.py — single source of truth.
# The SQL Server path still extracts the password from Lima when SQLSERVER_PASS
# is not set, since limactl is only available on the host (not in Docker).

dsn_postgres()    { "$VENV" benchmarks/bench_config.py dsn postgres;    }
dsn_cockroachdb() { "$VENV" benchmarks/bench_config.py dsn cockroachdb; }
dsn_mysql()       { "$VENV" benchmarks/bench_config.py dsn mysql;       }
dsn_oracle()      { "$VENV" benchmarks/bench_config.py dsn oracle;      }
dsn_db2()         { "$VENV" benchmarks/bench_config.py dsn db2;         }

dsn_sqlserver() {
    # Prefer the persistent password file written by the provision script.
    # Falls back to the cloud-init output log (legacy VMs without sentinel).
    if [[ -z "${SQLSERVER_PASS:-}" ]]; then
        local pass
        # New VMs: password persisted at /var/opt/mssql/.sa_password
        pass=$(limactl shell sqlserver22 -- \
                   sudo cat /var/opt/mssql/.sa_password 2>/dev/null || true)
        # Legacy fallback: read from cloud-init output log
        if [[ -z "$pass" ]]; then
            pass=$(limactl shell sqlserver22 -- \
                       sudo grep "SQL Server sa password is" /var/log/cloud-init-output.log 2>/dev/null \
                   | tail -1 | awk '{print $NF}' || true)
        fi
        [[ -n "$pass" ]] || die "SQL Server password unknown.  Set SQLSERVER_PASS in .env or start the sqlserver22 Lima VM."
        export SQLSERVER_PASS="$pass"
    fi
    "$VENV" benchmarks/bench_config.py dsn sqlserver
}

# export_bench_sqlserver_dsn
# Resolves the SQL Server password and exports BENCH_SQLSERVER_DSN so that
# run_all_bench.py / run_tpcb_bench.py pick it up at process start.
# Safe to call even when SQL Server is not in the target list — it warns and
# skips rather than dying.
export_bench_sqlserver_dsn() {
    if [[ -n "${BENCH_SQLSERVER_DSN:-}" ]]; then
        info "BENCH_SQLSERVER_DSN already set"
        return
    fi
    local pass="${SQLSERVER_PASS:-}"
    if [[ -z "$pass" ]]; then
        pass=$(limactl shell sqlserver22 -- \
                   sudo cat /var/opt/mssql/.sa_password 2>/dev/null || true)
    fi
    if [[ -z "$pass" ]]; then
        pass=$(limactl shell sqlserver22 -- \
                   sudo grep "SQL Server sa password is" /var/log/cloud-init-output.log 2>/dev/null \
               | tail -1 | awk '{print $NF}' || true)
    fi
    if [[ -n "$pass" ]]; then
        export BENCH_SQLSERVER_DSN="SERVER=127.0.0.1,${SQLSERVER_PORT:-14330};DATABASE=master;UID=sa;PWD=${pass}"
        info "SQL Server DSN resolved (port ${SQLSERVER_PORT:-14330})"
    else
        warn "SQL Server password unknown — SQL Server targets will be skipped by Python bench scripts"
    fi
}

# run_check_run OUT_FILE LOG_FILE DIALECT DSN [TARGET_DIALECT [TARGET_DSN]]
# ─────────────────────────────────────────────────────────────────────────────
# Post-run row-count integrity check.  Greps OUT_FILE for the "Result saved →"
# line written by identity_test.py, then calls check_run.py with the JSON path,
# log file, and connection details.
#
# Always non-fatal: a check failure prints a warning but does not exit.
# This keeps the shell scripts from aborting mid-run on a count mismatch.
#
# Arguments:
#   OUT_FILE        stdout captured from identity_test.py (contains the result path)
#   LOG_FILE        same file (or the .err file) for log scanning
#   DIALECT         source database dialect
#   DSN             source database DSN string
#   TARGET_DIALECT  (optional) target dialect; defaults to DIALECT
#   TARGET_DSN      (optional) target DSN; defaults to DSN
run_check_run() {
    local out_file="$1" log_file="$2" dialect="$3" dsn="$4"
    local tgt_dialect="${5:-$dialect}" tgt_dsn="${6:-$dsn}"

    # Extract the JSON result path from identity_test.py's "Result saved →" line.
    local json_path
    json_path=$(grep -o 'benchmarks/results/[^ ]*\.json' "$out_file" 2>/dev/null | tail -1 || true)

    if [[ -z "$json_path" ]]; then
        warn "check_run: no result JSON found in $out_file — skipping row-count check"
        return
    fi

    [[ -f "$json_path" ]] || { warn "check_run: $json_path not found — skipping"; return; }

    local extra_args=()
    if [[ "$tgt_dialect" != "$dialect" || "$tgt_dsn" != "$dsn" ]]; then
        extra_args+=("--target-dialect" "$tgt_dialect")
        [[ -n "$tgt_dsn" ]] && extra_args+=("--target-dsn" "$tgt_dsn")
    fi

    "$VENV" benchmarks/check_run.py \
        "$json_path" "$log_file" \
        --dialect "$dialect" \
        --dsn     "$dsn" \
        "${extra_args[@]}" \
    && true   # absorb non-zero exit; warn below
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        warn "check_run: row-count issues in $json_path (see output above)"
    fi
}

# ── Scale factors ─────────────────────────────────────────────────────────────
# Delegates to benchmarks/bench_config.py — single source of truth.

sf_for() {
    local engine="$1" schema="$2"
    "$VENV" benchmarks/bench_config.py sf_for "$engine" "$schema"
}
