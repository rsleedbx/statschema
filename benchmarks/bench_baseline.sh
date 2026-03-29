#!/usr/bin/env bash
# benchmarks/bench_baseline.sh
#
# Establish a clean CPU and process baseline before running benchmarks.
# Kills or waits for active benchmark tasks, stops unused QEMU VMs, and
# supports APFS-snapshot-based disk reset for fast clean database state.
#
# USAGE
#   bench_baseline.sh [--mode=MODE] [--engine=ENGINE] [--tag=TAG] [--help]
#
# MODES
#   isolate   (default) Force-stop QEMU VMs not required by --engine (~0.1s/VM).
#             Use before a focused A/B test on one engine.
#
#   clean     Gracefully stop the target engine then restart it for a fresh
#             SQL state (~5 min, VM reboot + crash recovery).
#             Use after a failed run that may have left dirty state.
#
#   snap      Take an APFS clonefile snapshot of the diffdisk for each QEMU VM
#             listed in --engine (or all if --engine=all).  VM must be stopped.
#             Creates ~/.lima/VM/diffdisk.snap-TAG.  Instant (zero-copy).
#
#   restore   Restore a previously snapped diffdisk from ~/.lima/VM/diffdisk.snap-TAG.
#             Force-stops the VM, atomically replaces diffdisk, then restarts.
#             Stop+restore: ~0.1s.  VM restart: ~2-5 min (no crash recovery
#             if snap was taken from a cleanly stopped VM).
#
#   nuke      Full Podman container teardown + fresh start for native engines
#             (postgres, cockroachdb, mysql).  Use when schema state is dirty.
#             Takes ~1-5s per container.
#
#   wait      Block until active benchmark processes finish, then confirm load.
#
# ENGINES
#   all           All three QEMU VMs  (default)
#   sqlserver     sqlserver22 only
#   oracle        oracle only
#   db2           db2 only
#   podman        Native Podman containers only (used with nuke mode)
#
# SNAP TAG
#   --tag=NAME    Snapshot name suffix (default: "baseline").
#                 Separate tags let you keep multiple restore points.
#
# EXAMPLES
#   # Before an A/B test of SQL Server only:
#   ./benchmarks/bench_baseline.sh --mode=isolate --engine=sqlserver
#
#   # Save a clean disk image after initial VM setup (takes <1s per VM):
#   ./benchmarks/bench_baseline.sh --mode=snap --engine=sqlserver --tag=clean
#
#   # Instantly reset SQL Server to that clean image (stop + swap disk + start):
#   ./benchmarks/bench_baseline.sh --mode=restore --engine=sqlserver --tag=clean
#
#   # Nuke and recreate all Podman containers:
#   ./benchmarks/bench_baseline.sh --mode=nuke --engine=podman
#
#   # Print current CPU/VM/container state without changing anything:
#   ./benchmarks/bench_baseline.sh --dry-run

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── defaults ──────────────────────────────────────────────────────────────────
MODE="isolate"
ENGINE="all"
SNAP_TAG="baseline"
DRY_RUN=0

# ── argument parsing ─────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --mode=*)    MODE="${arg#*=}"     ;;
        --engine=*)  ENGINE="${arg#*=}"   ;;
        --tag=*)     SNAP_TAG="${arg#*=}" ;;
        --dry-run)   DRY_RUN=1            ;;
        --help|-h)
            sed -n '3,56p' "$0"
            exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ── helpers ───────────────────────────────────────────────────────────────────
ts()   { echo "  [$(date '+%H:%M:%S')] $*"; }
info() { ts "$*"; }
warn() { ts "WARN: $*" >&2; }
run()  {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [DRY-RUN] $*"
    else
        eval "$@"
    fi
}

# ── host CPU snapshot ─────────────────────────────────────────────────────────
snapshot_cpu() {
    local cores
    cores=$(sysctl -n hw.physicalcpu 2>/dev/null || echo "?")
    echo ""
    echo "═══════════════════════════════════════════════════════"
    echo "  Host CPU snapshot  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "═══════════════════════════════════════════════════════"
    echo ""
    echo "  Physical cores: $cores"
    uptime
    echo ""
    echo "  QEMU threads (one per vCPU of running VMs):"
    if ps -eo pid,pcpu,comm 2>/dev/null | grep "qemu-system-x86_64" | grep -v grep; then
        local qemu_count
        qemu_count=$(ps -eo comm 2>/dev/null | grep -c "qemu-system-x86_64" || true)
        echo "  → $qemu_count QEMU threads consuming CPU"
    else
        echo "  → (none)"
    fi
    echo ""
    echo "  Top 5 CPU consumers:"
    { ps -eo pid,pcpu,comm -r 2>/dev/null || true; } | head -6 | tail -5 || true
    echo ""
    echo "  Lima VMs:"
    limactl list 2>/dev/null | grep -v "^NAME" || echo "  (limactl not found)"
    echo ""
    echo "  Podman containers:"
    podman ps --format "  {{.Names}}\t{{.Status}}" 2>/dev/null || echo "  (podman not found)"
    echo "═══════════════════════════════════════════════════════"
    echo ""
}

# ── kill or wait for active benchmark processes ───────────────────────────────
BENCH_PATTERN="run_matrix\.py|identity_test\.py|run_bench\.py|run_identity\.sh"

handle_benchmark_processes() {
    local pids
    pids=$(pgrep -f "$BENCH_PATTERN" 2>/dev/null || true)
    if [[ -z "$pids" ]]; then
        info "No active benchmark processes."
        return
    fi

    echo ""
    warn "Active benchmark processes detected:"
    pgrep -la "$BENCH_PATTERN" 2>/dev/null | sed 's/^/    /'

    if [[ "$MODE" == "wait" ]]; then
        info "Waiting for benchmark processes to finish (--mode=wait)…"
        local elapsed=0 max_wait=3600
        while pgrep -f "$BENCH_PATTERN" > /dev/null 2>&1; do
            if (( elapsed >= max_wait )); then
                warn "Benchmark processes still running after ${max_wait}s — giving up"
                return 1
            fi
            sleep 10; elapsed=$(( elapsed + 10 ))
            info "  still waiting… (${elapsed}s)"
        done
        info "Benchmark processes finished."
    else
        echo ""
        if [[ $DRY_RUN -eq 1 ]]; then
            echo "  [DRY-RUN] Would prompt: Kill benchmark processes? [y/N]"
            return
        fi
        read -rp "  Kill benchmark processes? [y/N] " answer
        if [[ "${answer,,}" == "y" ]]; then
            run pkill -f "$BENCH_PATTERN" || true
            sleep 2
            info "Killed benchmark processes."
        else
            info "Keeping benchmark processes — results may be skewed by contention."
        fi
    fi
}

# ── QEMU VM management ────────────────────────────────────────────────────────
ALL_QEMU_VMS=(sqlserver22 oracle db2)

vms_for_engine() {
    case "$ENGINE" in
        sqlserver) echo "sqlserver22" ;;
        oracle)    echo "oracle"      ;;
        db2)       echo "db2"         ;;
        all)       echo "sqlserver22 oracle db2" ;;
        podman)    echo ""            ;;
        *)         warn "Unknown engine '$ENGINE'"; echo "" ;;
    esac
}

is_vm_running() { limactl list 2>/dev/null | awk -v v="$1" '$1==v && $2=="Running" {found=1} END{exit !found}'; }

force_stop_vm() {
    local vm="$1"
    info "Force-stopping $vm…"
    run limactl stop --force "$vm" 2>/dev/null || true
}

stop_non_test_vms() {
    local -a keep
    read -ra keep <<< "$(vms_for_engine)"

    for vm in "${ALL_QEMU_VMS[@]}"; do
        is_vm_running "$vm" || continue
        local should_keep=0
        for k in "${keep[@]}"; do [[ "$vm" == "$k" ]] && should_keep=1 && break; done
        if [[ $should_keep -eq 0 ]]; then
            info "Force-stopping $vm (not under test)…"
            run limactl stop --force "$vm" 2>/dev/null || true
        else
            info "Keeping $vm (under test)."
        fi
    done
}

wait_qemu_clear() {
    local -a keep
    read -ra keep <<< "$(vms_for_engine)"
    local expected_threads=$(( ${#keep[@]} * 4 ))

    info "Waiting for stopped QEMU threads to exit (expecting ≤${expected_threads})…"
    local elapsed=0 max_wait=15
    while true; do
        local actual
        actual=$(ps -eo comm 2>/dev/null | grep -c "qemu-system-x86_64" || true)
        if (( actual <= expected_threads )); then
            info "QEMU threads cleared ($actual remaining, expected ≤$expected_threads)."
            return
        fi
        if (( elapsed >= max_wait )); then
            warn "QEMU threads still present after ${max_wait}s ($actual threads)."
            return
        fi
        sleep 2; elapsed=$(( elapsed + 2 ))
    done
}

start_vm() {
    local vm="$1"
    info "Starting $vm…"
    run limactl start "$vm" 2>&1 | grep -E "READY|MESSAGE|ERROR|started" || true
    info "$vm started."
}

# ── APFS clonefile snap / restore ─────────────────────────────────────────────
# macOS APFS supports zero-copy file clones (cp -c).  Creating and restoring
# a diffdisk snapshot takes <0.2s regardless of virtual disk size.
#
# Requirements:
#   - VM must be STOPPED before snap or restore (QEMU can't share a diffdisk).
#   - snapshots live alongside the diffdisk: ~/.lima/VM/diffdisk.snap-TAG
#
# Crash recovery note: if the snap was taken from a cleanly-stopped VM
# (graceful limactl stop), restoring it gives a crash-recovery-free restart.
# If taken from a force-killed VM, SQL Server will still need to run recovery.
# ALWAYS snap from a cleanly-stopped state.

LIMA_DIR="$HOME/.lima"

snap_vm() {
    local vm="$1" tag="$2"
    local diffdisk="$LIMA_DIR/$vm/diffdisk"
    local snap_file="$LIMA_DIR/$vm/diffdisk.snap-${tag}"

    [[ -f "$diffdisk" ]] || { warn "$vm: diffdisk not found at $diffdisk"; return 1; }

    if is_vm_running "$vm"; then
        warn "$vm is running — gracefully stopping before snap…"
        if [[ $DRY_RUN -eq 0 ]]; then
            limactl stop "$vm" 2>/dev/null || true
            sleep 3
        fi
    fi

    info "Snapping $vm → diffdisk.snap-${tag}  (APFS clonefile, zero-copy)…"
    local t0=$SECONDS
    run cp -c "$diffdisk" "$snap_file"
    local elapsed=$(( SECONDS - t0 ))

    local snap_size
    snap_size=$(ls -lsh "$snap_file" 2>/dev/null | awk '{print $1}' || echo "?")
    info "Snap created: $snap_file  (virtual ${snap_size}, took ${elapsed}s)"
}

restore_vm() {
    local vm="$1" tag="$2"
    local diffdisk="$LIMA_DIR/$vm/diffdisk"
    local snap_file="$LIMA_DIR/$vm/diffdisk.snap-${tag}"

    [[ -f "$snap_file" ]] || { warn "$vm: snapshot not found: $snap_file"; return 1; }

    if is_vm_running "$vm"; then
        info "Force-stopping $vm before restore…"
        run limactl stop --force "$vm" 2>/dev/null || true
        sleep 1
    fi

    info "Restoring $vm from diffdisk.snap-${tag}…"
    run cp -c "$snap_file" "${diffdisk}.restoring"
    run mv "${diffdisk}.restoring" "$diffdisk"
    info "Disk restored.  Starting $vm…"
    run limactl start "$vm" 2>&1 | grep -E "READY|MESSAGE|ERROR|started" || true
    info "$vm is up from snapshot '${tag}'."

    # SQL Server regenerates the SA password on every cloud-init run.
    # After a restore, sync the new password to .env so bench_config.py finds it.
    if [[ "$vm" == sqlserver* ]]; then
        sync_sqlserver_pass "$vm"
    fi
}

# sync_sqlserver_pass VM
# Extracts the current SA password from cloud-init-output.log inside the VM
# and writes it to SQLSERVER_PASS in the repo .env file.
sync_sqlserver_pass() {
    local vm="$1"
    local new_pass
    new_pass=$(limactl shell "$vm" -- bash -c \
        "sudo grep 'SQL Server sa password is' /var/log/cloud-init-output.log 2>/dev/null | tail -1 | awk '{print \$NF}'" 2>/dev/null || true)
    if [[ -z "$new_pass" ]]; then
        warn "Could not extract SA password from $vm cloud-init log — update SQLSERVER_PASS in .env manually."
        return
    fi
    local env_file="${REPO_ROOT}/.env"
    if [[ -f "$env_file" ]]; then
        if grep -q "^SQLSERVER_PASS=" "$env_file"; then
            sed -i.bak "s|^SQLSERVER_PASS=.*|SQLSERVER_PASS=${new_pass}|" "$env_file"
        else
            echo "SQLSERVER_PASS=${new_pass}" >> "$env_file"
        fi
        info "SQLSERVER_PASS synced to .env (${new_pass:0:8}…)"
    fi
    export SQLSERVER_PASS="$new_pass"
}

list_snaps() {
    echo ""
    echo "  Available diffdisk snapshots:"
    local found=0
    for vm in "${ALL_QEMU_VMS[@]}"; do
        local dir="$LIMA_DIR/$vm"
        [[ -d "$dir" ]] || continue
        while IFS= read -r f; do
            local tag="${f##*diffdisk.snap-}"
            local sz
            sz=$(ls -lsh "$f" 2>/dev/null | awk '{print $1}')
            local ts_str
            ts_str=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f" 2>/dev/null || echo "?")
            echo "    $vm  tag=$tag  virtual=${sz}  created=$ts_str"
            found=1
        done < <(find "$dir" -maxdepth 1 -name "diffdisk.snap-*" 2>/dev/null | sort)
    done
    [[ $found -eq 0 ]] && echo "    (none — run --mode=snap to create one)"
    echo ""
}

# ── Podman container nuke ─────────────────────────────────────────────────────
# Tear down a Podman container completely and restart it from a clean image.
# This is the equivalent of "delete and redeploy" for native containers.
# Measured time: ~1s for postgres (image already cached locally).

load_env() {
    local env_file="${REPO_ROOT}/.env"
    [[ -f "$env_file" ]] && set -o allexport && source <(grep -E '^[A-Z_][A-Z0-9_]*=' "$env_file") && set +o allexport || true
}

nuke_postgres() {
    local port="${PG18_PORT:-5418}"
    local pass="${PG18_PASS:-${PG_PASSWORD:-postgres}}"
    info "Nuking pg18 (stop → rm → run)…"
    run "podman stop pg18 2>/dev/null; podman rm pg18 2>/dev/null; \
         podman run -d --name pg18 -e POSTGRES_PASSWORD='$pass' -p ${port}:5432 postgres:18 2>/dev/null"
    local elapsed=0
    while ! nc -z 127.0.0.1 "$port" 2>/dev/null; do
        sleep 1; elapsed=$(( elapsed + 1 ))
        [[ $elapsed -ge 60 ]] && warn "pg18 port did not open after 60s" && return 1
    done
    info "pg18 up on port $port (${elapsed}s)"
}

nuke_mysql() {
    local port="${MYSQL8_PORT:-3384}"
    local pass="${MYSQL_ROOT_PASS:-testpass}"
    info "Nuking mysql8 (stop → rm → run)…"
    run "podman stop mysql8 2>/dev/null; podman rm mysql8 2>/dev/null; \
         podman run -d --name mysql8 -e MYSQL_ROOT_PASSWORD='$pass' -p ${port}:3306 mysql:8 2>/dev/null"
    local elapsed=0
    while ! nc -z 127.0.0.1 "$port" 2>/dev/null; do
        sleep 1; elapsed=$(( elapsed + 1 ))
        [[ $elapsed -ge 120 ]] && warn "mysql8 port did not open after 120s" && return 1
    done
    info "mysql8 up on port $port (${elapsed}s)"
}

nuke_cockroachdb() {
    local port="${CRDB_SINGLE_PORT:-26257}"
    info "Nuking crdb-single (stop → rm → run)…"
    run "podman stop crdb-single 2>/dev/null; podman rm crdb-single 2>/dev/null; \
         podman run -d --name crdb-single -p ${port}:${port} cockroachdb/cockroach:latest \
           start-single-node --insecure 2>/dev/null"
    local elapsed=0
    while ! nc -z 127.0.0.1 "$port" 2>/dev/null; do
        sleep 1; elapsed=$(( elapsed + 1 ))
        [[ $elapsed -ge 60 ]] && warn "crdb-single port did not open after 60s" && return 1
    done
    info "crdb-single up on port $port (${elapsed}s)"
}

# ── final load check ─────────────────────────────────────────────────────────
check_load_ok() {
    local cores load15
    cores=$(sysctl -n hw.physicalcpu 2>/dev/null || echo "8")
    load15=$(uptime | awk -F'load averages:|load average:' '{print $2}' | awk '{print $NF}' | tr -d ',')
    if awk -v l="$load15" -v c="$cores" 'BEGIN{exit !(l < c)}'; then
        info "Load average OK: ${load15} < ${cores} cores."
    else
        warn "Load average ${load15} >= ${cores} cores — host may be saturated."
    fi
}

# ── main ──────────────────────────────────────────────────────────────────────
_dry_label=""; [[ $DRY_RUN -eq 1 ]] && _dry_label="  [DRY-RUN]"
echo ""
info "bench_baseline.sh  mode=${MODE}  engine=${ENGINE}  tag=${SNAP_TAG}${_dry_label}"

snapshot_cpu

case "$MODE" in
    isolate)
        handle_benchmark_processes
        stop_non_test_vms
        wait_qemu_clear
        check_load_ok
        ;;

    clean)
        handle_benchmark_processes
        stop_non_test_vms
        wait_qemu_clear
        target_vms_str=$(vms_for_engine)
        read -ra _target_vms <<< "$target_vms_str"
        for vm in "${_target_vms[@]}"; do
            info "Clean mode: gracefully stopping $vm…"
            run limactl stop "$vm" 2>/dev/null || true
            sleep 2
            start_vm "$vm"
        done
        check_load_ok
        ;;

    snap)
        list_snaps
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            snap_vm "$vm" "$SNAP_TAG"
        done
        list_snaps
        ;;

    restore)
        list_snaps
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            restore_vm "$vm" "$SNAP_TAG"
        done
        check_load_ok
        ;;

    nuke)
        load_env
        handle_benchmark_processes
        case "$ENGINE" in
            postgres)    nuke_postgres    ;;
            cockroachdb) nuke_cockroachdb ;;
            mysql)       nuke_mysql       ;;
            podman|all)
                nuke_postgres    &
                nuke_mysql       &
                nuke_cockroachdb &
                wait
                ;;
            *) warn "nuke mode only supports podman engines (postgres, cockroachdb, mysql, all/podman)" ;;
        esac
        check_load_ok
        ;;

    wait)
        handle_benchmark_processes
        check_load_ok
        ;;

    list-snaps)
        list_snaps
        exit 0
        ;;

    *)
        echo "Unknown mode: $MODE (use isolate, clean, snap, restore, nuke, wait, list-snaps)" >&2
        exit 1
        ;;
esac

echo ""
info "Done."
echo ""
info "Final state:"
limactl list 2>/dev/null | awk 'NR==1 || /Running/' | sed 's/^/  /'
echo ""
