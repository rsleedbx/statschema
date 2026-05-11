# benchmarks/_common_lima.sh
#
# Lima-only variant of _common.sh.
# All containers route through Lima VMs — no host Podman, no Colima required.
# NOT executable directly — source it: source "$(dirname "$0")/_common.sh"
#
# Provides:
#   load_dotenv          — load .env from repo root (safe grep-based approach)
#   find_python          — sets VENV to the virtualenv Python; dies if missing
#   info / warn / die    — timestamped log helpers
#   wait_port            — wait until a TCP port is open (necessary but not sufficient)
#   wait_db              — wait until the engine accepts a real connection (use this)
#   start_pg             — start postgres container (version from DB_VERSION, port auto-discovered)
#   start_crdb    — start CockroachDB container if not up
#   start_mysql          — start MySQL 8 container if not up
#   start_sqlserver      — start SQL Server Podman container (Rosetta 2) if not up
#   start_oracle         — start Oracle Podman container if not up
#   start_db2            — start DB2 Podman container (Rosetta 2) if not up
#   start_databases      — Phase 0 orchestrator (all Lima VM containers, all parallel)
#   delete_container CONTAINER     — stop and remove a container (Podman or Lima db2lima)
#   delete_container_all CONTAINER — stop, remove container and its named volume (Podman or Lima db2lima)
#   sqlcli  CONTAINER    — interactive SQL shell as the app user
#   sqlclidba CONTAINER  — interactive SQL shell as the DBA user
#   sf_for ENGINE SCHEMA — print the scale factor for an engine × schema pair

# Auto-derive GIT_REPO_ROOT from the git repo root if the caller did not set it.
: "${GIT_REPO_ROOT:=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)}"

# Python package name — used when probing venvs for a usable interpreter.
_STATSCHEMA_PKG="statschema"

# ── .env loading ──────────────────────────────────────────────────────────────
# Exports only lines matching KEY=VALUE (skips comments, blank lines, lowercase).
# Called automatically on source; safe to call again from run_*.sh scripts.
load_dotenv() {
    local env_file="${GIT_REPO_ROOT:-.}/.env"
    if [[ -f "$env_file" ]]; then
        set -o allexport
        # shellcheck disable=SC1090
        source <(grep -E '^[A-Z_][A-Z0-9_]*=' "$env_file")
        set +o allexport
    fi
}
load_dotenv

# ── Lima VM name ──────────────────────────────────────────────────────────────
# The general-purpose Lima VM for all non-DB2 containers.
# Override via env var: LIMA_VM=myvm source _common_lima.sh
_LIMA_VM="${LIMA_VM:-statschema}"

# ── Lima VM helpers ───────────────────────────────────────────────────────────
# _lnerdctl: run a nerdctl command inside the statschema Lima VM.
# Uses sudo + full path because limactl shell uses a non-login shell that
# doesn't source /usr/local/bin into PATH.
_lnerdctl() { limactl shell "$_LIMA_VM" -- sudo /usr/local/bin/nerdctl "$@"; }

# ensure_lima: create and start the statschema Lima VM if not already running.
# Uses VZ + Rosetta 2 on Apple Silicon (fast x86_64 via Rosetta for sqlserver/oracle).
# Override VM startup via LIMA_START_ARGS.
ensure_lima() {
    local vm="$_LIMA_VM"

    # Fast path: already running.
    if limactl list 2>/dev/null | awk 'NR>1{print $1,$2}' | grep -q "^${vm} Running"; then
        _lima_ensure_containerd "$vm"
        return 0
    fi

    # Start existing stopped VM.
    if limactl list 2>/dev/null | awk 'NR>1{print $1}' | grep -q "^${vm}$"; then
        info "Starting stopped Lima VM '${vm}'..."
        limactl start "$vm" 2>&1 | sed 's/^/  [lima] /'
        return
    fi

    # Create from scratch using Lima's default template (containerd + nerdctl).
    info "Creating Lima VM '${vm}' (VZ + Rosetta 2, containerd + nerdctl)..."
    local _set_args=()
    if [[ "$(uname -m)" == "arm64" ]]; then
        _set_args=(
            --set '.vmType = "vz"'
            --set '.vmOpts.vz.rosetta.enabled = true'
            --set '.vmOpts.vz.rosetta.binfmt = true'
        )
    fi
    # shellcheck disable=SC2086
    limactl create --name "$vm" "${_set_args[@]}" \
        ${LIMA_START_ARGS:-} template:default \
        2>&1 | sed 's/^/  [lima] /' \
        || die "limactl create failed for VM '${vm}'"
    limactl start "$vm" 2>&1 | sed 's/^/  [lima] /' \
        || die "limactl start failed for VM '${vm}'"
    _lima_ensure_containerd "$vm"
    info "Lima VM '${vm}' is up (containerd + nerdctl)"
}

_lima_ensure_containerd() {
    # Enable and start containerd inside the Lima VM if not already active.
    # Lima's default template installs containerd but does not enable it on boot.
    local vm="${1:-$_LIMA_VM}"
    if limactl shell "$vm" -- sudo systemctl is-active containerd &>/dev/null; then
        return 0
    fi
    info "Starting containerd inside Lima VM '${vm}'..."
    limactl shell "$vm" -- sudo systemctl enable --now containerd 2>&1 \
        | sed 's/^/  [containerd] /' || warn "containerd start may have failed — check: limactl shell $vm -- sudo systemctl status containerd"
}

# ── Python virtualenv ─────────────────────────────────────────────────────────
# Sets VENV to the first Python interpreter that has statschema on sys.path.
# Preference order: .venv_test (CI/test venv) → .venv (dev venv).
# Dies with a helpful message if neither exists.
find_python() {
    local candidates=(
        "${GIT_REPO_ROOT}/.venv_test/bin/python"
        "${GIT_REPO_ROOT}/.venv/bin/python"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -x "$candidate" ]]; then
            if "$candidate" -c "import ${_STATSCHEMA_PKG}" 2>/dev/null; then
                VENV="$candidate"
                return
            fi
        fi
    done
    echo "ERROR: no venv with ${_STATSCHEMA_PKG} found.  Run: pip install -e . (dev) or make venv-test (CI)" >&2
    kill -INT $$
}

# ── Log helpers ───────────────────────────────────────────────────────────────
info() { echo "  [$(date '+%H:%M:%S')] $*"; }
warn() { echo "  [$(date '+%H:%M:%S')] WARN: $*" >&2; }
die()  { echo "  [$(date '+%H:%M:%S')] ERROR: $*" >&2; kill -INT $$; }

# ── find_free_port BASE_PORT ─────────────────────────────────────────────────
# Prints the first TCP port >= BASE_PORT that is:
#   (a) not actively listening on 127.0.0.1, AND
#   (b) not already reserved by any Podman container (running or stopped).
# Stopped containers don't bind their ports on the host but Podman still
# owns the reservation, so nc -z alone would hand out a conflicting port.
find_free_port() {
    local port="$1"
    # Collect all host ports already reserved by any Podman container.
    local _podman_ports
    _podman_ports=$(_lnerdctl inspect \
        --format '{{range $p, $b := .HostConfig.PortBindings}}{{(index $b 0).HostPort}} {{end}}' \
        $(_lnerdctl ps -aq 2>/dev/null) 2>/dev/null)
    while true; do
        # Skip if port is actively listening.
        nc -z 127.0.0.1 "$port" 2>/dev/null && { port=$(( port + 1 )); continue; }
        # Skip if port is already reserved by a stopped (or running) container.
        [[ " $_podman_ports " == *" $port "* ]] && { port=$(( port + 1 )); continue; }
        break
    done
    echo "$port"
}

# ── wait_port HOST PORT LABEL [MAX_SECONDS=120] ───────────────────────────────
wait_port() {
    local host="$1" port="$2" label="$3" max_s="${4:-120}"
    local elapsed=0
    while ! nc -z "$host" "$port" 2>/dev/null; do
        (( elapsed >= max_s )) && die "$label port $port did not open after ${max_s}s"
        sleep 5; elapsed=$(( elapsed + 5 ))
        info "  waiting for $label on $port ... (${elapsed}s)"
    done
    info "$label is up on $port"
}

# ── wait_db LABEL [MAX_S=120] [CONTAINER] CHECK_CMD [ARGS...] ────────────────
# Polls until the CHECK_CMD exits 0 — a real connection, not just TCP open.
#
# LABEL      — human-readable name for log/error messages
# MAX_S      — max seconds to poll before dying (default: 120)
# CONTAINER  — Podman container name; on each failed poll the last 5 log lines
#              are shown and all logs are saved to ${TMPDIR:-/tmp}/<name>-init.log
#              Pass "" to skip log capture.
# CHECK_CMD  — any command + args whose exit code indicates readiness
#
# NOTE: logs are NOT streamed in the background — running _lnerdctl logs -f and
# _lnerdctl exec simultaneously against the same container causes Podman to hang.
# Instead, a snapshot is captured via _lnerdctl logs --tail on each failure.
#
# Examples:
#   wait_db "postgres (pg18)" 60 "pg18" \
#       _lnerdctl exec pg18 pg_isready -U postgres
#   wait_db "SQL Server (sqlserver2022-latest)" 90 "sqlserver2022-latest" \
#       _lnerdctl exec sqlserver2022-latest /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P ... -Q "SELECT 1" -b
wait_db() {
    local label="$1" max_s="${2:-120}" container="${3:-}"
    shift 3
    local log_file=""
    [[ -n "$container" ]] && log_file="${TMPDIR:-/tmp}/${container}-init.log"

    local elapsed=0
    until "$@" &>/dev/null; do
        if (( elapsed >= max_s )); then
            # Dump full container log on timeout for post-mortem.
            if [[ -n "$container" ]]; then
                info "  last container logs saved → $log_file"
                _lnerdctl logs "$container" 2>&1 | tee "$log_file" | tail -20
            fi
            die "$label did not accept connections after ${max_s}s"
        fi
        sleep 10; elapsed=$(( elapsed + 10 ))
        info "  waiting for $label to accept connections... (${elapsed}s)"
        # Show a brief snapshot so the user can see what the container is doing.
        [[ -n "$container" ]] && _lnerdctl logs --tail 3 "$container" 2>&1 \
            | sed 's/^/    /'
    done

    # Save the full log on success too (quiet — no terminal noise).
    [[ -n "$container" ]] && _lnerdctl logs "$container" >"$log_file" 2>&1
    info "$label is accepting connections"
}

# ── _start_container ─────────────────────────────────────────────────────────
# Reusable helper: start (or reuse) a named Podman container.
#
# Usage:
#   _start_container CONTAINER IMAGE GUEST_PORT DEFAULT_PORT PORT_VAR \
#                    [RUN_OPT...] [-- CMD_ARG...]
#
#   CONTAINER     _lnerdctl container name (e.g. pg18, mysql8, crdb24)
#   IMAGE         image:tag (e.g. postgres:18)
#   GUEST_PORT    port exposed inside the container (e.g. 5432)
#   DEFAULT_PORT  host port to search from when creating a new container
#   PORT_VAR      name of the env var to export with the resolved host port
#   RUN_OPT...    extra options for _lnerdctl run before IMAGE (e.g. -e KEY=VAL)
#   -- CMD_ARG... optional container command/args placed after IMAGE

_start_container() {
    local container="$1" image="$2" guest_port="$3" default_port="$4" port_var="$5" data_mount="$6"
    shift 6

    # Split remaining args on '--': before is _lnerdctl run options, after is CMD.
    local run_opts=() cmd_args=() past_sep=0
    for arg in "$@"; do
        if [[ "$arg" == "--" ]]; then
            past_sep=1
        elif [[ $past_sep -eq 0 ]]; then
            run_opts+=("$arg")
        else
            cmd_args+=("$arg")
        fi
    done

    local _state port
    _state=$(_lnerdctl inspect --format '{{.State.Status}}' "$container" 2>/dev/null)

    if [[ -n "$_state" ]]; then
        # Container exists (running or stopped) — read its provisioned host port.
        port=$(_lnerdctl inspect --format \
            "{{(index (index .HostConfig.PortBindings \"${guest_port}/tcp\") 0).HostPort}}" \
            "$container" 2>/dev/null)
        port="${port:-${!port_var:-$default_port}}"
        if [[ "$_state" == "running" ]]; then
            info "$container already up on $port"
        else
            info "Starting stopped $container on port $port..."
            _lnerdctl start "$container" 2>/dev/null || true
            wait_port 127.0.0.1 "$port" "$container" 60
        fi
    else
        # Container does not exist — find a free port and create it.
        port=$(find_free_port "${!port_var:-$default_port}")
        local _vol_args=()
        if [[ "$data_mount" != "-" && -n "$data_mount" ]]; then
            _vol_args=(-v "${container}-data:${data_mount}")
            info "Creating $container on port $port (volume: ${container}-data -> ${data_mount})"
        else
            info "Creating $container on port $port (no named volume)"
        fi
        if ! _lnerdctl run -d --name "$container" \
               "${_vol_args[@]}" "${run_opts[@]}" \
               -p "${port}:${guest_port}" "$image" "${cmd_args[@]}"; then
            die "_lnerdctl run failed for $container (image: $image) — check image name, architecture, or pull errors above"
        fi
        wait_port 127.0.0.1 "$port" "$container" 60
    fi

    # Export so downstream callers see the resolved port.
    printf -v "$port_var" '%s' "$port"
    export "$port_var"
}

# ── Container / VM startup ────────────────────────────────────────────────────

start_pg() {
    # $1 / DB_VERSION           — postgres version tag (default: latest)
    # DB_PG_DBA_PASSWORD        — POSTGRES_PASSWORD inside container
    #   → DB_DBA_PASSWORD → Statsch3ma!
    # DB_PG_USERNAME            — app user name  → DB_USERNAME  → statschema
    # DB_PG_PASSWORD            — app user pass  → DB_PASSWORD  → Statsch3ma!
    # DB_PG_CATALOG             — database name  → DB_CATALOG   → statschema
    # PG_PORT                   — base port for search / exported resolved port
    local version="${1:-${DB_VERSION:-latest}}"
    local container="pg${version//latest/}"; container="${container//[.-]/}"
    local dba_pass="${DB_PG_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
    local user="${DB_PG_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_PG_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    local catalog="${DB_PG_CATALOG:-${DB_CATALOG:-statschema}}"

    # PG 18+ recommends mounting the parent /var/lib/postgresql so pg_upgrade
    # can use --link across major versions without crossing mount boundaries.
    # Older versions are also compatible with this mount point.
    _start_container "$container" "postgres:${version}" 5432 "${PG_PORT:-5432}" PG_PORT \
        /var/lib/postgresql \
        -e POSTGRES_PASSWORD="$dba_pass"

    # Wait for PostgreSQL to accept real connections (TCP open is not enough).
    # pg_isready: purpose-built health check; uses Unix socket (trust auth),
    # never reads stdin, exits 0 when the server is ready to accept connections.
    wait_db "postgres ($container)" 60 "$container" \
        _lnerdctl exec "$container" \
            pg_isready -U postgres

    # Provision: idempotently create the app user and catalog, grant access.
    # \$body\$ → shell produces $body$ (PostgreSQL dollar-quoting; avoids $$ = PID).
    # \gexec   → psql executes the returned string as SQL (CREATE DATABASE IF NOT EXISTS idiom).
    _provision_run "$container" \
        env PGPASSWORD="${dba_pass}" \
        psql -U postgres -v ON_ERROR_STOP=1 <<EOF || { warn "provision $container failed"; return 1; }
-- Create or update the app user.
DO \$body\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${user}') THEN
        CREATE USER ${user} WITH PASSWORD '${app_pass}';
    ELSE
        ALTER USER ${user} WITH PASSWORD '${app_pass}';
    END IF;
END
\$body\$;
-- Create the catalog database if it does not exist.
SELECT 'CREATE DATABASE ${catalog} OWNER ${user}'
  WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = '${catalog}'
  ) \gexec
-- Grant connect on the catalog.
GRANT CONNECT ON DATABASE ${catalog} TO ${user};
EOF
    info "Provisioned $container: user=${user} catalog=${catalog}"
}

start_crdb() {
    # $1 / CRDB_VERSION  — image tag (default: latest)
    # DB_CRDB_USERNAME   — app user name  → DB_USERNAME  → statschema
    # DB_CRDB_PASSWORD   — app user pass  → DB_PASSWORD  → Statsch3ma!
    # DB_CRDB_CATALOG    — database name  → DB_CATALOG   → statschema
    # CRDB_PORT          — base port for search / exported resolved port
    local version="${1:-${CRDB_VERSION:-latest}}"
    local container="crdb${version//latest/}"; container="${container//[.-]/}"
    local user="${DB_CRDB_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_CRDB_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    local catalog="${DB_CRDB_CATALOG:-${DB_CATALOG:-statschema}}"

    _start_container "$container" "cockroachdb/cockroach:${version}" \
        26257 "${CRDB_PORT:-26257}" CRDB_PORT \
        /cockroach/cockroach-data \
        -- start-single-node --insecure

    # Wait for CockroachDB to accept real connections (TCP open is not enough).
    wait_db "cockroachdb ($container)" 60 "$container" \
        _lnerdctl exec "$container" \
            /cockroach/cockroach sql --insecure -e "SELECT 1"

    # Provision: idempotently create the app user and catalog, grant access.
    # Passwords are not supported in --insecure mode; create user without one.
    _provision_run "$container" \
        /cockroach/cockroach sql --insecure --user=root <<EOF || { warn "provision $container failed"; return 1; }
-- Create user if not exists (no password in insecure mode).
CREATE USER IF NOT EXISTS ${user};
-- Create catalog if not exists.
CREATE DATABASE IF NOT EXISTS ${catalog};
-- Grant full access on the catalog.
GRANT ALL ON DATABASE ${catalog} TO ${user};
ALTER USER ${user} CREATEDB;
EOF
    info "Provisioned $container: user=${user} catalog=${catalog}"
}

start_mysql() {
    # $1 / MYSQL_VERSION          — image tag (default: latest)
    # DB_MYSQL_DBA_PASSWORD       — root password inside the container
    #   → DB_DBA_PASSWORD → Statsch3ma!
    # DB_MYSQL_USERNAME           — app user name  → DB_USERNAME  → statschema
    # DB_MYSQL_PASSWORD           — app user pass  → DB_PASSWORD  → Statsch3ma!
    # DB_MYSQL_CATALOG            — database name  → DB_CATALOG   → statschema
    # MYSQL8_PORT                 — base port for search / exported resolved port
    local version="${1:-${MYSQL_VERSION:-latest}}"
    local container="mysql${version//latest/}"; container="${container//[.-]/}"
    local dba_pass="${DB_MYSQL_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
    local user="${DB_MYSQL_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_MYSQL_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    local catalog="${DB_MYSQL_CATALOG:-${DB_CATALOG:-statschema}}"

    _start_container "$container" "mysql:${version}" \
        3306 "${MYSQL8_PORT:-3384}" MYSQL8_PORT \
        /var/lib/mysql \
        -e MYSQL_ROOT_PASSWORD="$dba_pass"

    # Wait for MySQL to accept real connections (TCP open is not enough).
    wait_db "mysql ($container)" 60 "$container" \
        _lnerdctl exec "$container" \
            mysqladmin ping -u root -p"${dba_pass}" --silent

    # Provision: idempotently create the app user and catalog, grant access.
    _provision_run "$container" \
        mysql -u root -p"${dba_pass}" <<EOF || { warn "provision $container failed"; return 1; }
-- Create catalog if not exists.
CREATE DATABASE IF NOT EXISTS \`${catalog}\`;
-- Create user if not exists; always refresh password.
CREATE USER IF NOT EXISTS '${user}'@'%' IDENTIFIED BY '${app_pass}';
ALTER USER '${user}'@'%' IDENTIFIED BY '${app_pass}';
-- Grant full access on the catalog.
GRANT ALL PRIVILEGES ON \`${catalog}\`.* TO '${user}'@'%';
FLUSH PRIVILEGES;
EOF
    info "Provisioned $container: user=${user} catalog=${catalog}"
}

# ── _resolve_container NAME → resolved_name ──────────────────────────────────
# Resolves a short alias to the actual Podman container name.
# Falls back to the name as-is if no alias matches.
#
# Aliases:
#   cockroach / cockroachdb / crdb  → first running crdb* container
_resolve_container() {
    local name="$1"
    case "$name" in
        cockroach|cockroachdb|crdb)
            # Resolve to the first existing crdb* container (e.g. crdb24, crdb25).
            local found
            found=$(_lnerdctl ps -a --format '{{.Names}}' 2>/dev/null \
                    | grep '^crdb' | head -1)
            echo "${found:-crdb}"
            ;;
        oracle|oracle-xe|oracle-free)
            # Resolve to the first existing oracle* container (e.g. oraclelatest, oracle23).
            local found
            found=$(_lnerdctl ps -a --format '{{.Names}}' 2>/dev/null \
                    | grep '^oracle' | head -1)
            echo "${found:-oracle23}"
            ;;
        *) echo "$name" ;;
    esac
}

# ── delete_container / delete_container_all CONTAINER ────────────────────────
# delete_container     CONTAINER  — stop and remove the container only.
# delete_container_all CONTAINER  — stop, remove the container AND its named
#                                   volume (${CONTAINER}-data for Podman;
#                                   db2_data inside the Lima VM for db2lima).
# CONTAINER may be a short alias (e.g. cockroach, crdb), the exact name, or
# the Lima target 'db2lima' (routes to db2ce inside the Lima 'db2' VM).
delete_container() {
    local name="${1:?Usage: delete_container CONTAINER}"

    # Lima DB2 — db2ce lives inside the Lima 'db2' VM, not the host Podman.
    if [[ "$name" == "db2lima" ]]; then
        if ! limactl shell db2 -- sudo podman inspect db2ce &>/dev/null; then
            warn "delete_container: db2ce not found inside Lima 'db2' VM"
            return 1
        fi
        limactl shell db2 -- sudo podman stop db2ce &>/dev/null || true
        limactl shell db2 -- sudo podman rm   db2ce &>/dev/null
        info "deleted Lima container: db2ce (VM: db2)"
        return
    fi


    local container
    container="$(_resolve_container "$name")"
    if [[ -z "$(_lnerdctl inspect --format '{{.Id}}' "$container" 2>/dev/null)" ]]; then
        warn "delete_container: no container named '$container' found"
        return 1
    fi
    _lnerdctl stop "$container" &>/dev/null || true
    _lnerdctl rm   "$container" &>/dev/null
    info "deleted container: $container"
}

delete_container_all() {
    local name="${1:?Usage: delete_container_all CONTAINER}"

    # Lima DB2 — container + volume both live inside the Lima 'db2' VM.
    # Volume name is 'db2_data' as defined in config/lima/db2.yaml.
    if [[ "$name" == "db2lima" ]]; then
        delete_container "db2lima" || true
        if limactl shell db2 -- sudo podman volume inspect db2_data &>/dev/null; then
            limactl shell db2 -- sudo podman volume rm db2_data &>/dev/null
            info "deleted Lima volume: db2_data (VM: db2)"
        else
            info "no Lima volume named 'db2_data' — skipping"
        fi
        return
    fi


    local container
    container="$(_resolve_container "$name")"
    local volume="${container}-data"
    delete_container "$container" || true
    if _lnerdctl volume inspect "$volume" &>/dev/null; then
        _lnerdctl volume rm "$volume" &>/dev/null
        info "deleted volume: $volume"
    else
        info "no volume named '$volume' — skipping"
    fi
}

start_sqlserver() {
    # $1 / SS_VERSION       — Docker tag (default: latest)
    # DB_SS_DBA_PASSWORD    — SA password        → DB_DBA_PASSWORD → Statsch3ma!
    # DB_SS_USERNAME        — app login name     → DB_USERNAME     → statschema
    # DB_SS_PASSWORD        — app login password → DB_PASSWORD     → Statsch3ma!
    # DB_SS_CATALOG         — database name      → DB_CATALOG      → statschema
    # SQLSERVER_PORT        — base host port for discovery (default: 1433)
    # at the time of testing, 2022 works.  2025 does not work with osx rs2
    local version="${1:-${SS_VERSION:-2022-latest}}"
    local container="sqlserver${version//latest/}"; container="${container//[.-]/}"
    local dba_pass="${DB_SS_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
    local user="${DB_SS_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_SS_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    local catalog="${DB_SS_CATALOG:-${DB_CATALOG:-statschema}}"
    local _ss_probe=(_lnerdctl exec "$container"
        /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$dba_pass" -Q "SELECT 1" -b)

    if ! "${_ss_probe[@]}" &>/dev/null; then
        # SQL Server has no native ARM64 image; Rosetta 2 translates x86_64.
        _start_container "$container" \
            "mcr.microsoft.com/mssql/server:${version}" \
            1433 "${SQLSERVER_PORT:-1433}" SQLSERVER_PORT \
            /var/opt/mssql \
            --platform linux/amd64 \
            -e ACCEPT_EULA=Y \
            -e MSSQL_SA_PASSWORD="$dba_pass"
        wait_db "SQL Server ($container)" 120 "$container" "${_ss_probe[@]}"
    else
        info "SQL Server ($container) is already accepting connections"
    fi

    # Provision: create login, catalog database, and app user.
    _provision_run "$container" \
        /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$dba_pass" <<EOF \
        || { warn "provision $container failed"; return 1; }
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '${user}')
    CREATE LOGIN [${user}] WITH PASSWORD = '${app_pass}';
GO
IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = '${catalog}')
    CREATE DATABASE [${catalog}];
GO
USE [${catalog}];
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '${user}')
    CREATE USER [${user}] FOR LOGIN [${user}];
ALTER ROLE db_owner ADD MEMBER [${user}];
GO
EOF
    info "Provisioned $container: user=${user} catalog=${catalog}"
}

start_oracle() {
    # $1 / ORACLE_VERSION         — free image tag (default: latest)
    # DB_ORA_DBA_PASSWORD         — SYS/SYSTEM password (Oracle complexity required)
    #   → DB_DBA_PASSWORD → Statsch3ma!
    # DB_ORA_USERNAME             — app user name → DB_USERNAME → statschema
    # DB_ORA_PASSWORD             — app user password → DB_PASSWORD → Statsch3ma!
    # DB_ORA_CATALOG              — PDB name → DB_CATALOG → FREEPDB1
    # ORACLE_PORT                 — base port for search / exported resolved port
    # https://blogs.oracle.com/developers/running-containers-with-colima
    local version="${1:-${ORACLE_VERSION:-latest}}"
    local container="oracle${version//latest/}"; container="${container//[.-]/}"
    local pass="${DB_ORA_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
    local app_user="${DB_ORA_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_ORA_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    # Probe via TCP as system — same path sqlclidba uses, so the probe matches
    # what the application actually needs.  OS-auth (/ as sysdba) is not used
    # because it can fail even when TCP connections are fully working.
    # -L: single login attempt — exits non-zero immediately on connection failure.
    # Without -L, sqlplus reads EXIT from stdin and exits 0 even when the PDB is
    # not yet registered (ORA-12514), producing a false-positive ready signal.
    local _ora_pdb="${DB_ORA_CATALOG:-${DB_ORA_DATABASE:-${ORACLE_PDB:-FREEPDB1}}}"
    local _ora_probe=(_lnerdctl exec "$container"
        sqlplus -L -S "system/${pass}@//localhost:1521/${_ora_pdb}")

    if "${_ora_probe[@]}" &>/dev/null; then
        info "$container is already accepting connections"
    else
        # Oracle can take ~60s to fully initialize on first boot.
        # APP_USER/APP_USER_PASSWORD are handled by the image on first boot only;
        # the explicit provisioning below is idempotent for subsequent runs.
        # Oracle Free has a native ARM64 image, but Podman on Apple Silicon defaults
        # to pulling the x86_64 manifest entry without an explicit platform flag.
        # Passing --platform linux/arm64 forces the native image, which is faster
        # and avoids Rosetta 2 emulation overhead.
        local _platform=()
        [[ "$(uname -m)" == "arm64" ]] && _platform=(--platform linux/arm64)
        _start_container "$container" \
            "container-registry.oracle.com/database/free:${version}" \
            1521 "${ORACLE_PORT:-1521}" ORACLE_PORT \
            /opt/oracle/oradata \
            --shm-size=1g \
            "${_platform[@]}" \
            -e ORACLE_PWD="$pass" \
            -e APP_USER="$app_user" \
            -e APP_USER_PASSWORD="$app_pass"
        wait_db "$container" 180 "$container" "${_ora_probe[@]}"
    fi

    # Provision: create/update app user and grant privileges in the PDB.
    # Idempotent — safe to re-run on every start_oracle call.
    # /nolog starts sqlplus without connecting; CONNECT follows inside the script.
    # WHENEVER SQLERROR EXIT FAILURE propagates connection/SQL errors as non-zero exit.
    # Password is double-quoted in CONNECT so special chars (e.g. !) are literal.
    _provision_run "$container" \
        sqlplus -L -S /nolog <<EOF \
        || { warn "provision $container failed"; return 1; }
WHENEVER SQLERROR EXIT FAILURE
CONNECT system/"${pass}"@//localhost:1521/${_ora_pdb}
DECLARE
  v_exists NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_exists FROM dba_users WHERE username = UPPER('${app_user}');
  IF v_exists = 0 THEN
    EXECUTE IMMEDIATE 'CREATE USER ${app_user} IDENTIFIED BY "${app_pass}"';
  ELSE
    EXECUTE IMMEDIATE 'ALTER USER ${app_user} IDENTIFIED BY "${app_pass}"';
  END IF;
END;
/
GRANT CONNECT, RESOURCE TO ${app_user};
GRANT UNLIMITED TABLESPACE TO ${app_user};
EXIT;
EOF
    info "Provisioned $container: user=${app_user} pdb=${_ora_pdb}"
}

start_db2() {
    # $1 / DB2_VERSION      — Docker tag (default: latest)
    # DB_DB2_DBA_PASSWORD   — db2inst1 password  → DB_DBA_PASSWORD → Statsch3ma!
    # DB_DB2_USERNAME       — app OS user        → DB_USERNAME     → statschema
    # DB_DB2_PASSWORD       — app user password  → DB_PASSWORD     → Statsch3ma!
    # DB_DB2_CATALOG        — DB name (≤8 chars) → DB_CATALOG      → statsch
    # DB2_PORT              — base host port for discovery (default: 50000)
    #
    # NOTE: TCP port open is insufficient — DB2 runs a ~300 s first-boot Task #3
    # (db2iupdt) during which the TCP port accepts but immediately resets.
    # We probe with a real "db2 connect" after the port is open.
    local version="${1:-${DB2_VERSION:-latest}}"
    local container="db2${version//latest/}"; container="${container//[.-]/}"
    local dba_pass="${DB_DB2_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
    local user="${DB_DB2_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_DB2_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
    # DB2 database names are limited to 8 characters.
    local catalog="${DB_DB2_CATALOG:-statsch}"
    # Fast probe: DB is already up and the catalog database exists.
    local _db2_probe=(_lnerdctl exec "$container"
        su - db2inst1 -c ". ~/sqllib/db2profile && db2 connect to ${catalog^^}")
    # Init-done probe: IBM's setup_db2_instance.sh writes this file as its very
    # last step — even when db2start/create-db fail inside the init (Rosetta 2).
    # We wait for this file before running our own provisioning, which handles
    # db2start and database creation idempotently.
    local _db2_init_done=(_lnerdctl exec "$container"
        test -f /database/config/.shared-data/setup_complete)

    if "${_db2_probe[@]}" &>/dev/null; then
        info "$container is already accepting connections"
    else
        # DB2 has no native ARM64 image; Rosetta 2 translates x86_64.
        # --ipc=host is required by IBM: db2ftok creates System V IPC keys for
        # shared-memory inter-process communication; an isolated IPC namespace
        # returns ENOTSUP (rc=0x870F00B4) for those operations under Rosetta 2.
        _start_container "$container" \
            "icr.io/db2_community/db2:${version}" \
            50000 "${DB2_PORT:-50000}" DB2_PORT \
            /database \
            --platform linux/amd64 \
            --privileged \
            --ipc=host \
            -e DB2INST1_PASSWORD="$dba_pass" \
            -e DBNAME="${catalog}" \
            -e LICENSE=accept

        # ── Rosetta 2 / ARM64 patch ──────────────────────────────────────────
        # Under x86_64 emulation on Apple Silicon, the IBM init scripts invoke
        #   su - db2inst1 -c 'db2 ...'
        # In that context, bash's login shell reads ~/.bash_profile → ~/.bashrc,
        # which DOES source db2profile — yet some su invocations during init
        # still fail with "db2: command not found" (transient Rosetta 2 IPC
        # state during first boot).  We patch every script in /var/db2_setup/
        # to prepend '. ~/sqllib/db2profile && ' as a belt-and-suspenders guard.
        # Note: '&&' in sed replacement must be '\&\&' (each '&' is a
        # metacharacter meaning "whole match"; '\&' escapes it to a literal '&').
        # Reference: https://community.ibm.com/community/user/discussion/db2-luw-115xx-mac-m1-ready
        info "Patching $container init scripts for Rosetta 2 compatibility..."
        _lnerdctl exec -i "$container" bash << 'PATCH' && info "Patch applied" || warn "Patch may have failed (non-fatal)"
for f in $(find /var/db2_setup -type f ! -name '.*' 2>/dev/null); do
    sed -i -E "s|su - [^ ]+ -c '|&. ~/sqllib/db2profile \&\& |g" "$f" 2>/dev/null || true
    sed -i -E 's|su - [^ ]+ -c "|&. ~/sqllib/db2profile \&\& |g' "$f" 2>/dev/null || true
done
PATCH

        # Wait for IBM init to write its setup_complete sentinel (up to 10 min).
        # The init may fail to start DB2 or create the database under Rosetta 2;
        # those steps are handled idempotently by the provisioning block below.
        wait_db "$container" 600 "$container" "${_db2_init_done[@]}"
    fi

    # Provision: ensure instance is up, database exists, app user exists with grants.
    # DBNAME env var only fires on first container boot; this block is idempotent.
    _lnerdctl exec "$container" bash -c "
        id ${user} &>/dev/null || useradd -m ${user}
        echo '${user}:${app_pass}' | chpasswd
    " || { warn "DB2 OS user creation for '${user}' failed"; return 1; }
    _provision_run "$container" \
        su - db2inst1 -c "
            . ~/sqllib/db2profile
            db2start 2>/dev/null; true
            db2 list db directory 2>/dev/null | grep -q ${catalog^^} \
                || db2 create db ${catalog^^}
            db2 connect to ${catalog^^}
            db2 \"GRANT CONNECT ON DATABASE TO USER ${user^^}\"
            db2 \"GRANT CREATETAB ON DATABASE TO USER ${user^^}\"
            db2 terminate
        " || { warn "provision $container failed"; return 1; }
    info "Provisioned $container: user=${user} catalog=${catalog^^}"
}

start_db2_lima() {
    # Starts (or resumes) the Lima 'db2' VM and provisions the statschema app user
    # inside the db2ce container managed by the VM's systemd service.
    #
    # Credential defaults match config/lima/db2.yaml (DB2INST1_PASSWORD=testpass,
    # DBNAME=statsch).  Override via .env:
    #   DB_DB2_DBA_PASSWORD  — db2inst1 password  (default: testpass)
    #   DB_DB2_CATALOG       — database name ≤8 chars (default: statsch)
    #   DB_DB2_USERNAME      — app OS/DB user        (default: statschema)
    #   DB_DB2_PASSWORD      — app user password     (default: Statsch3ma!)
    local dba_pass="${DB_DB2_DBA_PASSWORD:-testpass}"
    local catalog="${DB_DB2_CATALOG:-statsch}"
    local user="${DB_DB2_USERNAME:-${DB_USERNAME:-statschema}}"
    local app_pass="${DB_DB2_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"

    info "Starting Lima VM: db2"
    limactl start db2 2>/dev/null || true  # no-op if already running

    # Probe: real db2 connect inside db2ce — proves the instance is accepting SQL.
    # Pass "" as container arg so wait_db skips local _lnerdctl-logs capture.
    local _probe=(limactl shell db2 -- sudo podman exec db2ce
        su - db2inst1 -c ". ~/sqllib/db2profile && db2 connect to ${catalog^^}")
    wait_db "db2lima (${catalog^^})" 300 "" "${_probe[@]}"

    # Provision app OS user inside db2ce container.
    limactl shell db2 -- sudo podman exec db2ce bash -c "
        id ${user} &>/dev/null || useradd -m ${user}
        echo '${user}:${app_pass}' | chpasswd
    " || { warn "db2lima: OS user creation for '${user}' failed"; return 1; }

    # Grant DB access — idempotent, safe to re-run on every start_db2_lima call.
    limactl shell db2 -- sudo podman exec db2ce \
        su - db2inst1 -c "
            . ~/sqllib/db2profile
            db2start 2>/dev/null; true
            db2 connect to ${catalog^^}
            db2 \"GRANT CONNECT  ON DATABASE TO USER ${user^^}\"
            db2 \"GRANT CREATETAB ON DATABASE TO USER ${user^^}\"
            db2 terminate
        " || { warn "provision db2lima failed"; return 1; }

    info "Provisioned db2lima: user=${user} catalog=${catalog^^}"
}

# ── bench_prereq_check ────────────────────────────────────────────────────────
# Non-interactive baseline check sourced at the top of run_*.sh scripts.
# Warns (but does not abort) when active benchmark processes are detected or the
# host load average is at or above the physical core count.
# Call it before start_databases.
bench_prereq_check() {
    local cores load15 warn_issued=0

    cores=$(sysctl -n hw.physicalcpu 2>/dev/null || echo "?")
    load15=$(uptime | awk -F'load averages:|load average:' '{print $2}' | awk '{print $NF}' | tr -d ',')

    info "── Prereq check ──"

    local bench_pids
    bench_pids=$(pgrep -f "run_matrix\.py|identity_test\.py|run_bench\.py|run_identity\.sh" 2>/dev/null || true)
    if [[ -n "$bench_pids" ]]; then
        warn "Active benchmark processes detected (may skew results):"
        pgrep -la "run_matrix\.py|identity_test\.py|run_bench\.py|run_identity\.sh" 2>/dev/null \
            | sed 's/^/    /' || true
        warn "Run: benchmarks/bench_baseline.sh --mode=wait  to block until they finish."
        warn_issued=1
    fi

    if [[ "$cores" != "?" ]] && ! awk -v l="$load15" -v c="$cores" 'BEGIN{exit !(l < c)}' 2>/dev/null; then
        warn "Load average ${load15} >= ${cores} cores — host may be saturated."
        warn_issued=1
    fi

    [[ $warn_issued -eq 0 ]] && info "Host is clean.  Load: ${load15}/${cores}."
    info "────────────────────────────────────────────────────"
}

# start_databases ENGINE_COMMA_LIST
# Starts only the databases in the comma-separated list, all in parallel.
# All engines run inside Lima VMs (statschema VM via nerdctl, db2 via db2lima).
start_databases() {
    local engines="$1"
    local -a eng_list
    IFS=',' read -ra eng_list <<< "$engines"

    for e in "${eng_list[@]}"; do
        case "$e" in
            postgres)    start_pg          & ;;
            cockroachdb) start_crdb & ;;
            mysql)       start_mysql       & ;;
            sqlserver)   start_sqlserver   & ;;
            oracle)      start_oracle      & ;;
            db2)         start_db2         & ;;
            db2lima)     start_db2_lima    & ;;
        esac
    done
    wait
    info "All databases are up."
}


# ── sqlcli / sqlclidba CONTAINER ─────────────────────────────────────────────
# Open an interactive SQL shell inside the named Podman container or Lima VM.
#   sqlcli    — connects as the application user (DB_XX_USERNAME / DB_XX_PASSWORD)
#   sqlclidba — connects as the DBA / superuser
#
# Container name determines the engine:
#   pg*         → psql          (Podman)
#   mysql*      → mysql         (Podman)
#   crdb*       → cockroach sql (Podman)
#   sqlserver*  → sqlcmd        (_lnerdctl exec)
#   oracle*     → sqlplus       (_lnerdctl exec)
#   db2lima     → db2           (limactl shell db2 → sudo podman exec db2ce)
#   db2*        → db2           (_lnerdctl exec → su db2inst1)

_sqlcli_run() {
    # Adds -t (allocate PTY) only when stdin is an interactive terminal.
    # This lets the same function serve both interactive shells (sqlcli/sqlclidba)
    # and piped heredoc provisioning (_provision_run alias below).
    local target="$1"; shift
    local tty_flag=(); [[ -t 0 ]] && tty_flag=(-t)
    _lnerdctl exec -i "${tty_flag[@]}" "$target" "$@"
}

# Alias — same function, documents intent at the call site.
_provision_run() { _sqlcli_run "$@"; }

# Lima DB2 exec wrapper — routes through the Lima 'db2' VM instead of local _lnerdctl.
# Usage: _limacli_db2_run CMD [ARGS...]
_limacli_db2_run() {
    local tty_flag=(); [[ -t 0 ]] && tty_flag=(-t)
    limactl shell db2 -- sudo podman exec -i "${tty_flag[@]}" db2ce "$@"
}


sqlcli() {
    local container="${1:?Usage: sqlcli CONTAINER}"
    case "$container" in
        pg*)
            _sqlcli_run "$container" \
                env PGPASSWORD="${DB_PG_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}" \
                psql -U "${DB_PG_USERNAME:-${DB_USERNAME:-statschema}}" \
                     -d "${DB_PG_USERNAME:-${DB_USERNAME:-statschema}}"
            ;;
        mysql*)
            _sqlcli_run "$container" \
                mysql -u "${DB_MYSQL_USERNAME:-${DB_USERNAME:-statschema}}" \
                      -p"${DB_MYSQL_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}" \
                      "${DB_MYSQL_USERNAME:-${DB_USERNAME:-statschema}}"
            ;;
        crdb*)
            local _crdb_user="${DB_CRDB_USERNAME:-${DB_USERNAME:-statschema}}"
            local _crdb_db="${DB_CRDB_CATALOG:-${DB_CATALOG:-statschema}}"
            local _crdb_port="${CRDB_PORT:-26257}"
            _sqlcli_run "$container" \
                /cockroach/cockroach sql --insecure \
                --user="${_crdb_user}" --database="${_crdb_db}" --host="localhost:${_crdb_port}"
            ;;
        sqlserver*)
            _sqlcli_run "$container" \
                /opt/mssql-tools18/bin/sqlcmd -C \
                    -S localhost \
                    -U "${DB_SS_USERNAME:-${DB_USERNAME:-statschema}}" \
                    -P "${DB_SS_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}" \
                    -d "${DB_SS_CATALOG:-${DB_CATALOG:-statschema}}"
            ;;
        oracle*)
            local _ora_user="${DB_ORA_USERNAME:-${DB_USERNAME:-statschema}}"
            local _ora_pass="${DB_ORA_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
            local _ora_pdb="${DB_ORA_DATABASE:-${ORACLE_PDB:-FREEPDB1}}"
            _sqlcli_run "$container" \
                sqlplus "${_ora_user}/${_ora_pass}@//localhost:1521/${_ora_pdb}"
            ;;
        db2lima)
            local _db2_user="${DB_DB2_USERNAME:-${DB_USERNAME:-statschema}}"
            local _db2_pass="${DB_DB2_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
            local _db2_db="${DB_DB2_CATALOG:-statsch}"
            _limacli_db2_run \
                su - db2inst1 -c \
                    ". ~/sqllib/db2profile && db2start &>/dev/null; true && db2 \"CONNECT TO ${_db2_db^^} USER ${_db2_user} USING '${_db2_pass}'\" && db2"
            ;;
        db2*)
            local _db2_user="${DB_DB2_USERNAME:-${DB_USERNAME:-statschema}}"
            local _db2_pass="${DB_DB2_PASSWORD:-${DB_PASSWORD:-Statsch3ma!}}"
            local _db2_db="${DB_DB2_CATALOG:-statsch}"
            # db2start is needed after a container restart (instance not auto-started).
            # db2start exits non-zero (SQL1026N) when already active — suppress and continue.
            # Password is passed as a SQL string literal ('...') inside a single CLP
            # statement to avoid DB2 tokenizing special characters such as '!'.
            _sqlcli_run "$container" \
                su - db2inst1 -c \
                    ". ~/sqllib/db2profile && db2start &>/dev/null; true && db2 \"CONNECT TO ${_db2_db^^} USER ${_db2_user} USING '${_db2_pass}'\" && db2"
            ;;
        *)
            die "sqlcli: unknown container/VM '${container}' (expected pg*, mysql*, crdb*, sqlserver*, oracle*, db2lima, db2*)"
            ;;
    esac
}

sqlclidba() {
    local container="${1:?Usage: sqlclidba CONTAINER}"
    case "$container" in
        pg*)
            # DBA: postgres / DB_PG_DBA_PASSWORD → DB_DBA_PASSWORD → Statsch3ma!
            _sqlcli_run "$container" \
                env PGPASSWORD="${DB_PG_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}" \
                psql -U postgres -d postgres
            ;;
        mysql*)
            # DBA: root / DB_MYSQL_DBA_PASSWORD → DB_DBA_PASSWORD → Statsch3ma!
            _sqlcli_run "$container" \
                mysql -u root -p"${DB_MYSQL_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}"
            ;;
        crdb*)
            # DBA: root (no password in --insecure mode)
            _sqlcli_run "$container" \
                /cockroach/cockroach sql --insecure --user=root
            ;;
        sqlserver*)
            # DBA: sa / DB_SS_DBA_PASSWORD → DB_DBA_PASSWORD → Statsch3ma!
            _sqlcli_run "$container" \
                /opt/mssql-tools18/bin/sqlcmd -C \
                    -S localhost \
                    -U sa \
                    -P "${DB_SS_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}" \
                    -d master
            ;;
        oracle*)
            # DBA: system / DB_ORA_DBA_PASSWORD → DB_DBA_PASSWORD → Statsch3ma!
            local _ora_pdb="${DB_ORA_DATABASE:-${ORACLE_PDB:-FREEPDB1}}"
            _sqlcli_run "$container" \
                sqlplus "system/${DB_ORA_DBA_PASSWORD:-${DB_DBA_PASSWORD:-Statsch3ma!}}@//localhost:1521/${_ora_pdb}"
            ;;
        db2lima)
            # DBA: db2inst1 (OS auth inside Lima db2 VM → db2ce container).
            local _db2_db="${DB_DB2_CATALOG:-statsch}"
            _limacli_db2_run \
                su - db2inst1 -c \
                    ". ~/sqllib/db2profile && db2start &>/dev/null; true && db2 connect to ${_db2_db^^} && db2"
            ;;
        db2*)
            # DBA: db2inst1 (OS auth; no password needed once connected as the instance owner).
            # db2start ensures the instance is running after a container restart.
            local _db2_db="${DB_DB2_CATALOG:-statsch}"
            _sqlcli_run "$container" \
                su - db2inst1 -c \
                    ". ~/sqllib/db2profile && db2start &>/dev/null; true && db2 connect to ${_db2_db^^} && db2"
            ;;
        *)
            die "sqlclidba: unknown container/VM '${container}' (expected pg*, mysql*, crdb*, sqlserver*, oracle*, db2lima, db2*)"
            ;;
    esac
}

