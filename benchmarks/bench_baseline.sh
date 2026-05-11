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
#   setup     Idempotent full-environment setup: start local registry, seed
#             missing images, provision/resume Lima VMs (sqlserver22, oracle,
#             db2), start host Podman containers (pg18, mysql8, crdb-single)
#             with named volumes.  Safe to run repeatedly.
#
#   seed-registry  Pull base image on the Mac host, push to local registry.
#             Use before first provision or after limactl delete when no VM
#             is running to push from.
#
#   push-images  Push all Podman images from running VMs to the local registry
#             so they survive limactl delete.  commit-state pushes automatically.
#
#   ensure-registry  Start the local registry container if not running.
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

# ── local registry ────────────────────────────────────────────────────────────
# A local OCI registry on the host at localhost:5000 caches base images and
# committed state images.  Lima VMs access it via host.lima.internal:5000.
# Images survive limactl delete; reprovision pulls from the registry instead of
# the internet (1.5 GB SQL Server, 3 GB DB2 saved as local layer cache).
LOCAL_REGISTRY="localhost:5000"
VM_REGISTRY="host.lima.internal:5000"

# registry_path "mcr.microsoft.com/mssql/server:2022-latest" → "mssql/server:2022-latest"
# registry_path "statschema/sqlserver22:baseline"            → "statschema/sqlserver22:baseline"
registry_path() {
    local img="$1"
    local first="${img%%/*}"
    if [[ "$first" == *"."* ]]; then
        echo "${img#*/}"
    else
        echo "$img"
    fi
}

ensure_registry() {
    if podman ps --format "{{.Names}}" 2>/dev/null | grep -q "^local-registry$"; then
        return 0
    fi
    if podman ps -a --format "{{.Names}}" 2>/dev/null | grep -q "^local-registry$"; then
        info "Starting existing local-registry container…"
        run podman start local-registry
        sleep 2
        return 0
    fi
    info "Creating local registry at localhost:5000…"
    run podman volume create local-registry-data 2>/dev/null || true
    run podman run -d --name local-registry \
        -p 5000:5000 \
        -v local-registry-data:/var/lib/registry \
        registry:2
    sleep 2
    info "Local registry started."
}

snap_vm() {
    local vm="$1" tag="$2"

    if [[ "$(uname -s)" != "Darwin" ]]; then
        info "snap skipped on $(uname -s) — APFS clonefiles are macOS-only (Phase 1-B: use podman commit instead)"
        return 0
    fi

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

    if [[ "$(uname -s)" != "Darwin" ]]; then
        info "restore skipped on $(uname -s) — APFS clonefiles are macOS-only (Phase 1-B: use podman commit instead)"
        return 0
    fi

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

    # The provision script guards with a sentinel file
    # (/var/opt/mssql/.statschema-provisioned), which is on the diffdisk and
    # therefore restored with it.  Password generation is skipped on every start
    # after the first — including after an APFS restore.  This call only ensures
    # SQLSERVER_PASS is populated in .env (e.g. on a fresh checkout or first use
    # after the initial snap).
    if [[ "$vm" == sqlserver* ]]; then
        sync_sqlserver_pass "$vm"
    fi
}

# sync_sqlserver_pass VM
# SA password is now fixed (Option D: Podman container with MSSQL_SA_PASSWORD=Bench1pass!).
# No dynamic retrieval needed.  Kept as a no-op so existing callers continue to work.
sync_sqlserver_pass() {
    export SQLSERVER_PASS="${SQLSERVER_PASS:-Bench1pass!}"
}

# container_for_vm VM → the Podman container name running inside that VM
container_for_vm() {
    case "$1" in
        sqlserver22) echo "sqlserver22" ;;
        db2)         echo "db2ce"       ;;
        oracle)      echo "oracle-xe"   ;;
        *)           echo ""            ;;
    esac
}

# yaml_for_vm VM → the Lima YAML filename (without .yaml) under config/lima/
yaml_for_vm() {
    case "$1" in
        sqlserver22) echo "sqlserver22" ;;
        oracle)      echo "oracle"      ;;
        db2)         echo "db2"         ;;
        *)           echo ""            ;;
    esac
}

# start_or_resume_vm VM
# Idempotent: create from YAML if VM doesn't exist, start if stopped, no-op if running.
start_or_resume_vm() {
    local vm="$1"
    local status
    status=$(limactl list 2>/dev/null | awk -v v="$vm" '$1==v {print $2}')

    case "$status" in
        Running)
            info "$vm already running"
            ;;
        Stopped|Broken)
            info "Starting stopped $vm…"
            limactl start "$vm"
            ;;
        "")
            local yaml
            yaml=$(yaml_for_vm "$vm")
            [[ -n "$yaml" ]] || { warn "start_or_resume_vm: unknown VM '$vm'"; return 1; }
            info "Provisioning $vm from config/lima/${yaml}.yaml…"
            limactl start --name="$vm" "${REPO_ROOT}/config/lima/${yaml}.yaml"
            ;;
    esac
}

# registry_has IMAGE_PATH → 0 if tag exists in local registry, 1 otherwise
# IMAGE_PATH is e.g. "mssql/server:2022-latest"
registry_has() {
    local path="$1"
    local repo="${path%:*}"
    local tag="${path##*:}"
    curl -sf "http://${LOCAL_REGISTRY}/v2/${repo}/tags/list" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if '${tag}' in (d.get('tags') or []) else 1)" \
        2>/dev/null
}

# base_image_for_vm VM → the upstream OCI image tag used in the YAML
base_image_for_vm() {
    case "$1" in
        sqlserver22) echo "mcr.microsoft.com/mssql/server:2022-latest" ;;
        db2)         echo "icr.io/db2_community/db2:latest"            ;;
        oracle)      echo "docker.io/gvenzl/oracle-xe:21-slim"         ;;
        *)           echo ""                                            ;;
    esac
}

# seed_registry VM
# Pull the base image on the Mac host's Podman store, then push it to the local
# registry at localhost:5000.  Both tags (docker.io/... and localhost:5000/...)
# share the same image layers — no extra disk cost.  The host copy is kept so
# re-seeding after a registry wipe is instant (no re-pull needed).
# Requires ~/.config/containers/registries.conf to trust localhost:5000 as insecure.
seed_registry() {
    local vm="$1"
    local upstream
    upstream=$(base_image_for_vm "$vm")
    [[ -n "$upstream" ]] || { warn "seed_registry: unknown VM '$vm'"; return 1; }

    ensure_registry

    local path
    path=$(registry_path "$upstream")
    local local_tag="${LOCAL_REGISTRY}/${path}"

    # Step 1: pull into host's Podman store (no-op if already present)
    info "Pulling $upstream into host Podman store…"
    run podman pull "$upstream"

    # Step 2: tag for local registry
    info "Tagging → ${local_tag}…"
    run podman tag "$upstream" "$local_tag"

    # Step 3: push to local registry — Lima VMs pull from here on provision
    info "Pushing → ${local_tag}…"
    run podman push --tls-verify=false "$local_tag"

    info "Registry seeded: ${local_tag}"
    info "Host store keeps: ${upstream}  (re-seed is instant if registry is wiped)"
}

# push_to_registry VM
# Push every Podman image in the VM to the local registry at host.lima.internal:5000.
# Images survive limactl delete and reprovision pulls from the registry instead
# of the internet.  Run once after first successful provision and again after
# committing a new state image (commit_state already calls this automatically).
push_to_registry() {
    local vm="$1"
    ensure_registry

    local images
    images=$(limactl shell "$vm" -- sudo podman images \
             --format "{{.Repository}}:{{.Tag}}" 2>/dev/null \
             | grep -v "^<none>" || true)

    if [[ -z "$images" ]]; then
        warn "push_to_registry: no images found in $vm"
        return
    fi

    while IFS= read -r img; do
        local path
        path=$(registry_path "$img")
        local remote="${VM_REGISTRY}/${path}"
        info "Pushing $img → ${remote}…"
        run limactl shell "$vm" -- sudo bash -c \
            "podman push --tls-verify=false '$img' '$remote'"
        info "Pushed: ${remote}"
    done <<< "$images"
}

# commit_state VM TAG
# Stop the DB cleanly inside the container, commit the filesystem to a local image,
# then restart.  Image is stored in Podman's image store inside the VM — no registry needed.
# Clean stop ensures no crash recovery is needed on the next podman run.
commit_state() {
    local vm="$1" tag="$2"
    local ctr
    ctr=$(container_for_vm "$vm")
    [[ -n "$ctr" ]] || { warn "commit_state: unknown VM '$vm'"; return 1; }

    local image="statschema/${ctr}:${tag}"
    info "Stopping $ctr inside $vm for clean commit…"
    run limactl shell "$vm" -- podman stop "$ctr"

    info "Committing $ctr → $image …"
    run limactl shell "$vm" -- sudo podman commit "$ctr" "$image"
    info "Committed: $image"

    local remote="${VM_REGISTRY}/$(registry_path "$image")"
    info "Pushing $image → ${remote}…"
    run limactl shell "$vm" -- sudo bash -c \
        "podman push --tls-verify=false '$image' '$remote'"
    info "Pushed to local registry: ${remote}"

    info "Restarting $ctr…"
    run limactl shell "$vm" -- podman start "$ctr"

    # Wait for port to come back
    local port
    case "$vm" in
        sqlserver22) port=14330 ;;
        db2)         port=50000 ;;
        oracle)      port=1521  ;;
    esac
    if [[ -n "$port" ]]; then
        local elapsed=0
        until limactl shell "$vm" -- nc -z localhost "$port" 2>/dev/null; do
            sleep 5; elapsed=$(( elapsed + 5 ))
            [[ $elapsed -ge 120 ]] && warn "$vm port $port did not reopen after 120s" && return 1
        done
        info "$vm is up on port $port after commit."
    fi
}

# restore_image VM TAG
# Replace the running container with a fresh one started from a committed image.
# Volume data comes from the committed image snapshot; the named volume is reused.
restore_image() {
    local vm="$1" tag="$2"
    local ctr
    ctr=$(container_for_vm "$vm")
    [[ -n "$ctr" ]] || { warn "restore_image: unknown VM '$vm'"; return 1; }

    local image="statschema/${ctr}:${tag}"
    info "Restoring $vm from $image…"

    # Stop and remove the running container; leave named volume intact
    run limactl shell "$vm" -- podman stop "$ctr" 2>/dev/null || true
    run limactl shell "$vm" -- podman rm -f "$ctr"

    # Re-run using the committed image with the same flags as the original provisioning
    case "$vm" in
        sqlserver22)
            run limactl shell "$vm" -- podman run -d \
                --name "$ctr" \
                --cap-add cap_net_bind_service \
                -p 14330:14330 \
                -e ACCEPT_EULA=Y \
                -e MSSQL_SA_PASSWORD=Bench1pass! \
                -e MSSQL_TCP_PORT=14330 \
                -e MSSQL_MEMORY_LIMIT_MB=5632 \
                -v sqlserver_data:/var/opt/mssql:Z \
                -v /tmp/lima:/tmp/lima \
                "$image"
            ;;
        db2)
            run limactl shell "$vm" -- sudo podman run -d \
                --name "$ctr" \
                --privileged=true \
                -p 50000:50000 \
                -e LICENSE=accept \
                -e DB2INST1_PASSWORD=testpass \
                -e DBNAME=statsch \
                -e ARCHIVE_LOGS=false \
                -e AUTOCONFIG=false \
                -v db2_data:/database:Z \
                -v /tmp/lima:/tmp/lima \
                "$image"
            ;;
        oracle)
            run limactl shell "$vm" -- sudo podman run -d \
                --name "$ctr" \
                --platform linux/amd64 \
                -p 1521:1521 \
                -p 5500:5500 \
                -e ORACLE_PASSWORD=oracle \
                -v oracle_data:/opt/oracle/oradata \
                "$image"
            ;;
    esac
    info "Container $ctr started from $image."
}

list_committed_images() {
    echo ""
    echo "  Committed podman images (statschema/*):"
    local found=0
    for vm in "${ALL_QEMU_VMS[@]}"; do
        is_vm_running "$vm" || continue
        local out
        out=$(limactl shell "$vm" -- sudo podman images \
                --format "  $vm  {{.Repository}}:{{.Tag}}  {{.Size}}" 2>/dev/null \
              | grep "statschema/" || true)
        if [[ -n "$out" ]]; then
            echo "$out"
            found=1
        fi
    done
    [[ $found -eq 0 ]] && echo "    (none — run --mode=commit-state to create one)"
    echo ""
}

list_registry_images() {
    echo ""
    echo "  Local registry (localhost:5000) contents:"
    if ! curl -sf "http://localhost:5000/v2/_catalog" >/dev/null 2>&1; then
        echo "    (registry not running — run --mode=ensure-registry)"
        echo ""
        return
    fi
    local repos
    repos=$(curl -sf "http://localhost:5000/v2/_catalog" 2>/dev/null \
        | python3 -c "import sys, json; [print(r) for r in json.load(sys.stdin).get('repositories', [])]" \
        2>/dev/null || true)
    if [[ -z "$repos" ]]; then
        echo "    (no images — run --mode=push-images first)"
        echo ""
        return
    fi
    while IFS= read -r repo; do
        [[ -n "$repo" ]] || continue
        local tags
        tags=$(curl -sf "http://localhost:5000/v2/${repo}/tags/list" 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join(d.get('tags') or []))" \
            2>/dev/null || echo "?")
        printf "  %-52s  tags: %s\n" "$repo" "$tags"
    done <<< "$repos"
    echo ""
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

# ── Podman container management ───────────────────────────────────────────────
# nuke_*: full teardown including volume (clean slate, used by --mode=nuke)
# ensure_*: idempotent start with named volume (used by --mode=setup)

load_env() {
    local env_file="${REPO_ROOT}/.env"
    [[ -f "$env_file" ]] && set -o allexport && source <(grep -E '^[A-Z_][A-Z0-9_]*=' "$env_file") && set +o allexport || true
}

_wait_port() {
    local name="$1" host="$2" port="$3" max="${4:-60}"
    local elapsed=0
    until nc -z "$host" "$port" 2>/dev/null; do
        sleep 1; elapsed=$(( elapsed + 1 ))
        [[ $elapsed -ge $max ]] && warn "$name port $port not open after ${max}s" && return 1
    done
    info "$name up on port $port (${elapsed}s)"
}

# ── nuke (stop + rm container + rm volume → fresh container + fresh volume) ──

nuke_postgres() {
    local port="${PG18_PORT:-5418}" pass="${PG18_PASS:-${PG_PASSWORD:-postgres}}"
    info "Nuking pg18…"
    run "podman stop pg18 2>/dev/null; podman rm pg18 2>/dev/null
         podman volume rm pg18_data 2>/dev/null || true
         podman volume create pg18_data
         podman run -d --name pg18 -e POSTGRES_PASSWORD='$pass' \
           -p ${port}:5432 -v pg18_data:/var/lib/postgresql postgres:18"
    _wait_port pg18 127.0.0.1 "$port"
}

nuke_mysql() {
    local port="${MYSQL8_PORT:-3384}" pass="${MYSQL_ROOT_PASS:-testpass}"
    info "Nuking mysql8…"
    run "podman stop mysql8 2>/dev/null; podman rm mysql8 2>/dev/null
         podman volume rm mysql8_data 2>/dev/null || true
         podman volume create mysql8_data
         podman run -d --name mysql8 -e MYSQL_ROOT_PASSWORD='$pass' \
           -p ${port}:3306 -v mysql8_data:/var/lib/mysql mysql:8"
    _wait_port mysql8 127.0.0.1 "$port" 120
}

nuke_cockroachdb() {
    local port="${CRDB_SINGLE_PORT:-26257}"
    info "Nuking crdb-single…"
    run "podman stop crdb-single 2>/dev/null; podman rm crdb-single 2>/dev/null
         podman volume rm crdb_data 2>/dev/null || true
         podman volume create crdb_data
         podman run -d --name crdb-single -p ${port}:${port} \
           -v crdb_data:/cockroach/cockroach-data \
           cockroachdb/cockroach:latest start-single-node --insecure"
    _wait_port crdb-single 127.0.0.1 "$port"
}

# ── ensure (idempotent: no-op if running, start if stopped, create if absent) ──

_container_has_volume() {
    # _container_has_volume CONTAINER VOLUME_NAME → 0 if volume is mounted
    podman inspect "$1" --format "{{json .Mounts}}" 2>/dev/null \
        | python3 -c "import sys,json; mounts=json.load(sys.stdin); exit(0 if any(m.get('Name')=='$2' for m in mounts) else 1)" \
        2>/dev/null
}

ensure_postgres() {
    local port="${PG18_PORT:-5418}" pass="${PG18_PASS:-${PG_PASSWORD:-postgres}}"
    if podman ps --format "{{.Names}}" 2>/dev/null | grep -q "^pg18$" \
       && _container_has_volume pg18 pg18_data; then
        info "pg18 already running with volume"; return
    fi
    info "Starting pg18…"
    podman volume create pg18_data 2>/dev/null || true
    podman rm -f pg18 2>/dev/null || true
    podman run -d --name pg18 -e POSTGRES_PASSWORD="$pass" \
        -p ${port}:5432 -v pg18_data:/var/lib/postgresql postgres:18
    _wait_port pg18 127.0.0.1 "$port"
}

ensure_mysql() {
    local port="${MYSQL8_PORT:-3384}" pass="${MYSQL_ROOT_PASS:-testpass}"
    if podman ps --format "{{.Names}}" 2>/dev/null | grep -q "^mysql8$" \
       && _container_has_volume mysql8 mysql8_data; then
        info "mysql8 already running with volume"; return
    fi
    info "Starting mysql8…"
    podman volume create mysql8_data 2>/dev/null || true
    podman rm -f mysql8 2>/dev/null || true
    podman run -d --name mysql8 -e MYSQL_ROOT_PASSWORD="$pass" \
        -p ${port}:3306 -v mysql8_data:/var/lib/mysql mysql:8
    _wait_port mysql8 127.0.0.1 "$port" 120
}

ensure_cockroachdb() {
    local port="${CRDB_SINGLE_PORT:-26257}"
    if podman ps --format "{{.Names}}" 2>/dev/null | grep -q "^crdb-single$" \
       && _container_has_volume crdb-single crdb_data; then
        info "crdb-single already running with volume"; return
    fi
    info "Starting crdb-single…"
    podman volume create crdb_data 2>/dev/null || true
    podman rm -f crdb-single 2>/dev/null || true
    podman run -d --name crdb-single -p ${port}:${port} \
        -v crdb_data:/cockroach/cockroach-data \
        cockroachdb/cockroach:latest start-single-node --insecure
    _wait_port crdb-single 127.0.0.1 "$port"
}

# ── setup_all: idempotent full-environment setup ──────────────────────────────
# Starts registry, seeds missing images, provisions Lima VMs, starts host containers.
# Safe to run repeatedly — each step is a no-op when already satisfied.

setup_all() {
    echo ""
    info "=== Step 1: Local registry ==="
    ensure_registry

    echo ""
    info "=== Step 2: Seed registry for Lima VMs (skip if already present) ==="
    for vm in sqlserver22 oracle db2; do
        local upstream path
        upstream=$(base_image_for_vm "$vm")
        path=$(registry_path "$upstream")
        if registry_has "$path"; then
            info "  $path ✓ (already in registry)"
        else
            info "  $path missing — seeding…"
            seed_registry "$vm"
        fi
    done

    echo ""
    info "=== Step 3: Lima VMs (sqlserver22, oracle, db2) ==="
    for vm in sqlserver22 oracle db2; do
        start_or_resume_vm "$vm" &
    done
    wait
    info "All Lima VMs are up."

    echo ""
    info "=== Step 4: Host Podman containers (pg18, mysql8, crdb-single) ==="
    load_env
    ensure_postgres     &
    ensure_mysql        &
    ensure_cockroachdb  &
    wait

    echo ""
    info "=== Environment ready ==="
    limactl list 2>/dev/null | awk 'NR==1 || /sqlserver22|oracle|db2/' | sed 's/^/  /'
    echo ""
    podman ps --format "  {{.Names}}\t{{.Status}}" 2>/dev/null \
        | grep -E "pg18|mysql8|crdb-single" || true
    list_registry_images
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

    commit-state)
        list_committed_images
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            commit_state "$vm" "$SNAP_TAG"
        done
        list_committed_images
        ;;

    restore-image)
        list_committed_images
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            restore_image "$vm" "$SNAP_TAG"
        done
        check_load_ok
        ;;

    setup)
        setup_all
        exit 0
        ;;

    ensure-registry)
        ensure_registry
        list_registry_images
        exit 0
        ;;

    seed-registry)
        # Pull base image on host, push to local registry, remove host copy.
        # Use for engines whose image isn't already inside a running VM
        # (e.g. oracle before first provision, or after limactl delete).
        ensure_registry
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            seed_registry "$vm"
        done
        list_registry_images
        ;;

    push-images)
        # Push all Podman images from the VM to the local registry so they
        # survive limactl delete.  Run once after first healthy provision and
        # again after commit-state (commit-state pushes automatically).
        ensure_registry
        read -ra _target_vms <<< "$(vms_for_engine)"
        for vm in "${_target_vms[@]}"; do
            push_to_registry "$vm"
        done
        list_registry_images
        ;;

    list-images)
        list_registry_images
        exit 0
        ;;

    list-snaps)
        list_snaps
        list_committed_images
        list_registry_images
        exit 0
        ;;

    *)
        echo "Unknown mode: $MODE (use setup, isolate, clean, snap, restore, commit-state, restore-image, seed-registry, push-images, ensure-registry, nuke, wait, list-snaps, list-images)" >&2
        exit 1
        ;;
esac

echo ""
info "Done."
echo ""
info "Final state:"
limactl list 2>/dev/null | awk 'NR==1 || /Running/' | sed 's/^/  /'
echo ""
