#!/usr/bin/env bash
# benchmarks/setup_app_schemas.sh
#
# Install application-specific schemas into running database instances so
# that all app.* tests in run_sweep.py pass instead of being skipped.
#
# For QEMU engines (SQL Server, Oracle): downloads and loads schemas into
# the running state1 VM.  With --snap, gracefully stops and takes a
# "state2" APFS clonefile snapshot to preserve the work permanently.
#
# For Podman engines (PostgreSQL, MySQL): starts Gitea / Mautic sidecar
# containers that auto-create their schemas on first boot.  Idempotent —
# skips any container or database that already exists.
#
# USAGE
#   setup_app_schemas.sh [--engine=ENGINE] [--snap] [--dry-run] [--help]
#
# ENGINES
#   all        (default) All engines
#   sqlserver  AdventureWorks 2022 + AdventureWorksLT 2022 + Chinook
#   oracle     Oracle HR + CO sample schemas
#   postgres   Gitea (~112 tables, PostgreSQL)
#   mysql      Mautic 5 (~108 tables, MySQL)
#
# OPTIONS
#   --snap     Take a state2 APFS snapshot after loading (QEMU only).
#              Equivalent to:
#                bench_baseline.sh --mode=snap --engine=ENGINE --tag=state2
#              Requires the VM to be stopped cleanly first — this script does
#              that automatically.
#   --dry-run  Print what would be done without executing.
#
# DOWNLOAD CACHE
#   Large files (AdventureWorks .bak, Chinook SQL, Oracle SQL scripts) are
#   cached in ~/.cache/statschema/ on first download and reused on every
#   subsequent run — no re-downloading.
#
# PREREQUISITES
#   state1 APFS snapshot (or equivalent running instance) for QEMU engines.
#   pg16 and mysql8 Podman containers running for Podman engines.
#
# EXAMPLES
#   # Set up all app schemas (QEMU engines must be running from state1)
#   ./benchmarks/setup_app_schemas.sh
#
#   # SQL Server only — load schemas and immediately snapshot as state2
#   ./benchmarks/setup_app_schemas.sh --engine=sqlserver --snap
#
#   # Just start Gitea + Mautic sidecars (no snapshotting)
#   ./benchmarks/setup_app_schemas.sh --engine=postgres
#   ./benchmarks/setup_app_schemas.sh --engine=mysql

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIMA_DIR="$HOME/.lima"
CACHE_DIR="${HOME}/.cache/statschema"

# ── argument parsing ─────────────────────────────────────────────────────────
ENGINE="all"
DO_SNAP=0
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --engine=*)  ENGINE="${arg#*=}"  ;;
        --snap)      DO_SNAP=1           ;;
        --dry-run)   DRY_RUN=1          ;;
        --help|-h)
            sed -n '3,53p' "$0"
            exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ── helpers ───────────────────────────────────────────────────────────────────
ts()   { echo "  [$(date '+%H:%M:%S')] $*"; }
info() { ts "$*"; }
warn() { ts "WARN: $*" >&2; }

# run CMD [ARGS...] — execute command array directly (preserves multi-word args).
# For shell-operator idioms (pipes, ||, &&) use: run bash -c "cmd || true"
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [DRY-RUN] $*"
    else
        "$@"
    fi
}

# cached_download URL FILENAME — download to ~/.cache/statschema/ only if absent.
# Prints only the local path on stdout; all status messages go to stderr so
# the path can be captured cleanly with $(...).
cached_download() {
    local url="$1" filename="$2"
    local dest="${CACHE_DIR}/${filename}"
    mkdir -p "$CACHE_DIR"
    if [[ -f "$dest" ]]; then
        ts "Using cached ${filename}  (${CACHE_DIR})" >&2
    else
        ts "Downloading ${filename}…" >&2
        if [[ $DRY_RUN -eq 0 ]]; then
            curl -fsSL "$url" -o "$dest"
            ts "Saved to cache: ${dest}" >&2
        else
            ts "[DRY-RUN] curl -fsSL '${url}' -o '${dest}'" >&2
        fi
    fi
    echo "$dest"
}

# Block until a TCP port becomes reachable (max $3 seconds)
wait_port() {
    local host="$1" port="$2" timeout="${3:-120}" label="${4:-$2}"
    local elapsed=0
    while ! nc -z "$host" "$port" 2>/dev/null; do
        sleep 2; elapsed=$(( elapsed + 2 ))
        [[ $elapsed -ge $timeout ]] && warn "$label did not open after ${timeout}s" && return 1
    done
    info "$label reachable (${elapsed}s)"
}

load_env() {
    local env_file="${REPO_ROOT}/.env"
    [[ -f "$env_file" ]] && set -o allexport \
        && source <(grep -E '^[A-Z_][A-Z0-9_]*=' "$env_file") \
        && set +o allexport || true
}

is_vm_running() {
    limactl list 2>/dev/null | awk -v v="$1" '$1==v && $2=="Running" {found=1} END{exit !found}'
}

# ── SQL Server: AdventureWorks + Chinook ──────────────────────────────────────
setup_sqlserver() {
    load_env
    local port="${SQLSERVER_PORT:-14330}"
    local pass="${SQLSERVER_PASS:-}"
    local vm="sqlserver22"

    if ! is_vm_running "$vm"; then
        warn "$vm is not running.  Start it from a state1 snapshot first:"
        warn "  ./benchmarks/bench_baseline.sh --mode=restore --engine=sqlserver --tag=state1"
        return 1
    fi

    if [[ -z "$pass" ]]; then
        warn "SQLSERVER_PASS not set in .env — cannot connect."
        return 1
    fi

    # ── AdventureWorks ──────────────────────────────────────────────────────
    info "Checking AdventureWorks databases…"
    local aw_exists
    aw_exists=$(sqlcmd -S "127.0.0.1,${port}" -U sa -P "$pass" -C -h -1 -Q \
        "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.databases WHERE name IN ('AdventureWorksLT2022','AdventureWorks2022');" \
        2>/dev/null | tr -d ' ' || echo "0")

    if [[ "$aw_exists" == "2" ]]; then
        info "AdventureWorks databases already present — skipping."
    else
        local awlt_bak aw_bak
        awlt_bak=$(cached_download \
            "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksLT2022.bak" \
            "AdventureWorksLT2022.bak")
        aw_bak=$(cached_download \
            "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks2022.bak" \
            "AdventureWorks2022.bak")

        # Stage via the shared filesystem so the SQL Server container can read it.
        # STATSCHEMA_CLIENT_STAGING_DIR defaults to /tmp/lima for Lima dev setups
        # where /tmp/lima is bind-mounted into the SQL Server container.
        local _staging_root="${STATSCHEMA_CLIENT_STAGING_DIR:-/tmp/lima}"
        info "Staging .bak files via ${_staging_root} (shared with container)…"
        mkdir -p "${_staging_root}"
        cp "$awlt_bak" "${_staging_root}/AdventureWorksLT2022.bak"
        cp "$aw_bak"   "${_staging_root}/AdventureWorks2022.bak"
        chmod 644 "${_staging_root}/AdventureWorksLT2022.bak" "${_staging_root}/AdventureWorks2022.bak"

        # The SQL Server container sees STATSCHEMA_SERVER_STAGING_DIR (may differ
        # from the client path when using NFS or non-Lima bind-mounts).
        local _server_root="${STATSCHEMA_SERVER_STAGING_DIR:-${_staging_root}}"
        info "Restoring AdventureWorksLT2022 and AdventureWorks2022…"
        run sqlcmd -S "127.0.0.1,${port}" -U sa -P "$pass" -C -Q "
RESTORE DATABASE [AdventureWorksLT2022]
FROM DISK = '${_server_root}/AdventureWorksLT2022.bak'
WITH MOVE 'AdventureWorksLT2022_Data' TO '/var/opt/mssql/data/AdventureWorksLT2022.mdf',
     MOVE 'AdventureWorksLT2022_Log'  TO '/var/opt/mssql/data/AdventureWorksLT2022_log.ldf',
     REPLACE;
RESTORE DATABASE [AdventureWorks2022]
FROM DISK = '${_server_root}/AdventureWorks2022.bak'
WITH MOVE 'AdventureWorks2022'     TO '/var/opt/mssql/data/AdventureWorks2022.mdf',
     MOVE 'AdventureWorks2022_log' TO '/var/opt/mssql/data/AdventureWorks2022_log.ldf',
     REPLACE;
"
        # Clean up staging files after successful restore
        rm -f "${_staging_root}/AdventureWorksLT2022.bak" "${_staging_root}/AdventureWorks2022.bak"
        info "AdventureWorks restored."
    fi

    # ── Chinook ─────────────────────────────────────────────────────────────
    info "Checking Chinook database…"
    local chinook_exists
    chinook_exists=$(sqlcmd -S "127.0.0.1,${port}" -U sa -P "$pass" -C -h -1 -Q \
        "SET NOCOUNT ON; SELECT COUNT(*) FROM sys.databases WHERE name = 'Chinook';" \
        2>/dev/null | tr -d ' ' || echo "0")

    if [[ "$chinook_exists" == "1" ]]; then
        info "Chinook database already present — skipping."
    else
        local chinook_sql
        chinook_sql=$(cached_download \
            "https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_SqlServer.sql" \
            "Chinook_SqlServer.sql")
        info "Loading Chinook…"
        run sqlcmd -S "127.0.0.1,${port}" -U sa -P "$pass" -C -i "$chinook_sql"
        info "Chinook loaded."
    fi

    if [[ $DO_SNAP -eq 1 ]]; then
        _snap_vm "$vm" "state2"
    fi
}

# ── Oracle: HR + CO sample schemas ───────────────────────────────────────────
setup_oracle() {
    local vm="oracle"

    if ! is_vm_running "$vm"; then
        warn "$vm is not running.  Start it from a state1 snapshot first:"
        warn "  ./benchmarks/bench_baseline.sh --mode=restore --engine=oracle --tag=state1"
        return 1
    fi

    info "Checking Oracle HR schema…"
    local hr_exists
    hr_exists=$("${REPO_ROOT}/.venv_test/bin/python" -c "
import oracledb
try:
    c = oracledb.connect(user='system', password='oracle', dsn='127.0.0.1:1521/XE')
    cur = c.cursor()
    cur.execute(\"SELECT COUNT(*) FROM dba_users WHERE username IN ('HR','CO')\")
    print(cur.fetchone()[0])
except Exception:
    print('0')
" 2>/dev/null || echo "0")

    if [[ "$hr_exists" == "2" ]]; then
        info "Oracle HR and CO schemas already present — skipping."
    else
        local hr_sql co_sql
        hr_sql=$(cached_download \
            "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/human_resources/hr_create.sql" \
            "hr_create.sql")
        co_sql=$(cached_download \
            "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/customer_orders/co_create.sql" \
            "co_create.sql")

        info "Creating Oracle HR and CO users and loading schemas…"
        HR_SQL="$hr_sql" CO_SQL="$co_sql" \
        run "${REPO_ROOT}/.venv_test/bin/python" - << 'PYEOF'
import oracledb, re, os

def run_sql_script(conn, script_path, skip_views=True):
    with open(script_path) as f:
        content = f.read()
    content = re.sub(r'^(SET|Prompt|SPOOL|HOST|COLUMN|TTITLE|PAUSE|DEFINE|ACCEPT|REMARK)\b.*$',
                     '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'^rem\b.*$', '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'--.*$', '', content, flags=re.MULTILINE)
    cur = conn.cursor()
    for stmt in [s.strip() for s in content.split(';') if len(s.strip()) > 5]:
        if skip_views and re.match(r'CREATE\s+OR\s+REPLACE\s+VIEW', stmt, re.I):
            continue
        if re.match(r'COMMENT\s+ON', stmt, re.I):
            continue
        try:
            cur.execute(stmt)
            conn.commit()
        except Exception as e:
            if 'ORA-00955' not in str(e):
                print(f"SKIP: {str(e)[:80]}")

sys_conn = oracledb.connect(user='system', password='oracle', dsn='127.0.0.1:1521/XE')
cur = sys_conn.cursor()
for user in ['hr', 'oe', 'co']:
    try:
        cur.execute(f'DROP USER {user} CASCADE')
    except Exception:
        pass
for stmt in [
    "CREATE USER hr IDENTIFIED BY hr",
    "GRANT CONNECT, RESOURCE, CREATE VIEW TO hr",
    "ALTER USER hr QUOTA UNLIMITED ON USERS",
    "CREATE USER co IDENTIFIED BY co",
    "GRANT CONNECT, RESOURCE, CREATE VIEW, CREATE SEQUENCE TO co",
    "ALTER USER co QUOTA UNLIMITED ON USERS",
]:
    cur.execute(stmt)
sys_conn.commit()
sys_conn.close()

run_sql_script(oracledb.connect(user='hr', password='hr', dsn='127.0.0.1:1521/XE'), os.environ['HR_SQL'])
run_sql_script(oracledb.connect(user='co', password='co', dsn='127.0.0.1:1521/XE'), os.environ['CO_SQL'])
print("HR and CO schemas created")
PYEOF
        info "Oracle HR and CO loaded."
    fi

    if [[ $DO_SNAP -eq 1 ]]; then
        _snap_vm "$vm" "state2"
    fi
}

# ── PostgreSQL: Gitea sidecar ─────────────────────────────────────────────────
setup_postgres() {
    local pg_port="${PG16_PORT:-5416}"

    if ! nc -z 127.0.0.1 "$pg_port" 2>/dev/null; then
        warn "pg16 is not reachable on port $pg_port — start it first:"
        warn "  podman run -d --name pg16 -e POSTGRES_PASSWORD=postgres -p 5416:5432 postgres:16"
        return 1
    fi

    # Create gitea user + database (idempotent)
    info "Creating gitea user and database in pg16 (idempotent)…"
    run podman exec pg16 psql -U postgres -c \
        "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gitea') THEN CREATE USER gitea WITH PASSWORD 'gitea123'; END IF; END \$\$;"
    if ! podman exec pg16 psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='gitea'" 2>/dev/null | grep -q 1; then
        run podman exec pg16 psql -U postgres -c "CREATE DATABASE gitea OWNER gitea;"
    else
        info "gitea database already exists."
    fi

    # Dedicated network so gitea can reach pg16 by name
    info "Setting up gitea_net Podman network…"
    run bash -c "podman network create gitea_net 2>/dev/null || true"
    run bash -c "podman network connect gitea_net pg16 2>/dev/null || true"

    # Start Gitea sidecar (idempotent)
    if podman inspect gitea &>/dev/null 2>&1; then
        local gitea_state
        gitea_state=$(podman inspect gitea --format '{{.State.Status}}' 2>/dev/null || echo "unknown")
        if [[ "$gitea_state" == "running" ]]; then
            info "Gitea container already running — skipping."
            return 0
        else
            info "Gitea container exists but not running — removing and recreating…"
            run bash -c "podman rm -f gitea 2>/dev/null || true"
        fi
    fi

    info "Pulling and starting Gitea…"
    podman image exists docker.io/gitea/gitea:latest \
        && info "Gitea image already present — skipping pull." \
        || run podman pull docker.io/gitea/gitea:latest
    run podman run -d --name gitea --network gitea_net -p 3000:3000 \
        -e GITEA__database__DB_TYPE=postgres \
        -e GITEA__database__HOST=pg16:5432 \
        -e GITEA__database__NAME=gitea \
        -e GITEA__database__USER=gitea \
        -e GITEA__database__PASSWD=gitea123 \
        -e GITEA__security__INSTALL_LOCK=true \
        -e GITEA__security__SECRET_KEY=statschema_test_secret_key_32chars0 \
        -e GITEA__server__ROOT_URL=http://localhost:3000/ \
        docker.io/gitea/gitea:latest

    info "Waiting for Gitea schema creation (~30s)…"
    wait_port 127.0.0.1 3000 60 "gitea"
    run sleep 20

    info "Creating Gitea admin user…"
    run podman exec -u git gitea gitea admin user create \
        --admin --username=gitadmin --password='Gitpass123!' \
        --email=admin@example.com --must-change-password=false \
        || info "Admin user may already exist — continuing."

    info "Gitea ready."
}

# ── MySQL: Mautic sidecar ─────────────────────────────────────────────────────
setup_mysql() {
    local mysql_port="${MYSQL8_PORT:-3384}"
    local mysql_pass="${MYSQL_ROOT_PASS:-testpass}"

    if ! nc -z 127.0.0.1 "$mysql_port" 2>/dev/null; then
        warn "mysql8 is not reachable on port $mysql_port — start it first."
        return 1
    fi

    # Create mautic database + user (idempotent, each statement on its own -e)
    info "Creating mautic database and user in mysql8 (idempotent)…"
    run podman exec mysql8 mysql -uroot -p"$mysql_pass" \
        -e "CREATE DATABASE IF NOT EXISTS mautic CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" \
        -e "CREATE USER IF NOT EXISTS 'mautic'@'%' IDENTIFIED BY 'mauticpass';" \
        -e "GRANT ALL PRIVILEGES ON mautic.* TO 'mautic'@'%';" \
        -e "FLUSH PRIVILEGES;"

    # Dedicated network so mautic can reach mysql8 by name
    info "Setting up mautic_net Podman network…"
    run bash -c "podman network create mautic_net 2>/dev/null || true"
    run bash -c "podman network connect mautic_net mysql8 2>/dev/null || true"

    # Start Mautic sidecar (idempotent)
    if podman inspect mautic &>/dev/null 2>&1; then
        local mautic_state
        mautic_state=$(podman inspect mautic --format '{{.State.Status}}' 2>/dev/null || echo "unknown")
        if [[ "$mautic_state" == "running" ]]; then
            local table_count
            table_count=$(podman exec mysql8 mysql -umautic -pmauticpass mautic \
                -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='mautic';" \
                2>/dev/null | tail -1 | tr -d ' ' || echo "0")
            if [[ "$table_count" -gt 50 ]]; then
                info "Mautic container running and schema present (${table_count} tables) — skipping."
                return 0
            fi
        else
            info "Mautic container exists but not running — removing and recreating…"
            run bash -c "podman rm -f mautic 2>/dev/null || true"
        fi
    fi

    info "Pulling and starting Mautic 5…"
    podman image exists docker.io/mautic/mautic:5-apache \
        && info "Mautic image already present — skipping pull." \
        || run podman pull docker.io/mautic/mautic:5-apache
    run podman run -d \
        --name mautic \
        --network mautic_net \
        -p 8080:80 \
        -e MAUTIC_DB_HOST=mysql8 \
        -e MAUTIC_DB_PORT=3306 \
        -e MAUTIC_DB_NAME=mautic \
        -e MAUTIC_DB_USER=mautic \
        -e MAUTIC_DB_PASSWORD=mauticpass \
        docker.io/mautic/mautic:5-apache

    info "Waiting for Mautic container to start…"
    wait_port 127.0.0.1 8080 120 "mautic"
    run sleep 10

    info "Running Mautic install (CLI)…"
    run podman exec mautic php /var/www/html/bin/console mautic:install \
        --force \
        --db_driver=pdo_mysql \
        --db_host=mysql8 \
        --db_port=3306 \
        --db_name=mautic \
        --db_user=mautic \
        --db_password=mauticpass \
        --admin_email=admin@example.com \
        '--admin_password=Mautic1234!' \
        --admin_firstname=Admin \
        --admin_lastname=User \
        "http://127.0.0.1:8080"

    info "Fixing Doctrine migration metadata…"
    run podman exec mautic bash -c \
        "cd /var/www/html && php bin/console doctrine:migrations:sync-metadata-storage && php bin/console doctrine:migrations:version --add --all --no-interaction && php bin/console cache:warmup"

    info "Inserting test contacts…"
    run podman exec mysql8 mysql -umautic -pmauticpass mautic \
        -e "INSERT IGNORE INTO leads (is_published,date_added,date_modified,date_identified,firstname,lastname,email,company,points) VALUES (1,NOW(),NOW(),NOW(),'Alice','Anderson','alice@example.com','Acme Corp',10),(1,NOW(),NOW(),NOW(),'Bob','Baker','bob@example.com','Beta Inc',20),(1,NOW(),NOW(),NOW(),'Carol','Clark','carol@example.com','Contoso Ltd',15),(1,NOW(),NOW(),NOW(),'David','Davis','david@example.com','Dunder Mifflin',5),(1,NOW(),NOW(),NOW(),'Eva','Evans','eva@example.com','Extensive Ent',30),(1,NOW(),NOW(),NOW(),'Frank','Fisher','frank@example.com','Fabco Inc',25),(1,NOW(),NOW(),NOW(),'Grace','Garcia','grace@example.com','Gamma Corp',8),(1,NOW(),NOW(),NOW(),'Henry','Harris','henry@example.com','Heroic Ltd',12),(1,NOW(),NOW(),NOW(),'Irene','Irving','irene@example.com','Ideal Systems',18),(1,NOW(),NOW(),NOW(),'James','Johnson','james@example.com','Jubilee Corp',22);"

    info "Mautic ready."
}

# ── APFS snap helper ─────────────────────────────────────────────────────────
_snap_vm() {
    local vm="$1" tag="$2"
    local diffdisk="$LIMA_DIR/$vm/diffdisk"
    local snap_file="$LIMA_DIR/$vm/diffdisk.snap-${tag}"

    info "Gracefully stopping $vm before taking snapshot…"
    limactl stop "$vm" 2>/dev/null || true
    sleep 3

    info "Snapping $vm → diffdisk.snap-${tag}  (APFS clonefile, zero-copy)…"
    run cp -c "$diffdisk" "$snap_file"

    local snap_size
    snap_size=$(ls -lsh "$snap_file" 2>/dev/null | awk '{print $1}' || echo "?")
    info "Snapshot saved: $snap_file  (virtual ${snap_size})"
    info "To restore later: ./benchmarks/bench_baseline.sh --mode=restore --engine=${vm%%22} --tag=${tag}"

    info "Restarting $vm…"
    limactl start "$vm" 2>&1 | grep -E "READY|MESSAGE|ERROR|started" || true
}

# ── main ──────────────────────────────────────────────────────────────────────
_dry_label=""; [[ $DRY_RUN -eq 1 ]] && _dry_label="  [DRY-RUN]"
echo ""
info "setup_app_schemas.sh  engine=${ENGINE}  snap=${DO_SNAP}  cache=${CACHE_DIR}${_dry_label}"
echo ""

load_env

case "$ENGINE" in
    sqlserver) setup_sqlserver ;;
    oracle)    setup_oracle    ;;
    postgres)  setup_postgres  ;;
    mysql)     setup_mysql     ;;
    all)
        case_failed=0
        setup_sqlserver || { warn "SQL Server setup failed"; case_failed=1; }
        setup_oracle    || { warn "Oracle setup failed";     case_failed=1; }
        setup_postgres  || { warn "PostgreSQL setup failed"; case_failed=1; }
        setup_mysql     || { warn "MySQL setup failed";      case_failed=1; }
        [[ $case_failed -ne 0 ]] && exit 1
        ;;
    *)
        echo "Unknown engine: $ENGINE (use sqlserver, oracle, postgres, mysql, all)" >&2
        exit 1 ;;
esac

echo ""
info "App schema setup complete."
info "Cache contents:"
ls -lsh "${CACHE_DIR}/" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"
echo ""
