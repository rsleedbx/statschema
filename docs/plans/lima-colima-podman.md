# Lima, Colima, and Podman on Apple Silicon: The Two-VM Reality

A practical guide for engineers who need x86_64 databases (DB2, Oracle, SQL Server)
running alongside ARM-native databases (Postgres, MySQL, CockroachDB) on the same Mac.

---

## 1. What each tool actually is

```
┌─────────────────────────────────────────────────────────────────────┐
│                          macOS (host)                               │
│                                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────────────┐   │
│  │  Lima    │   │  Colima  │   │     Podman Machine           │   │
│  │          │   │          │   │                              │   │
│  │ VM       │   │ VM mgr   │   │  VM manager                  │   │
│  │ manager  │   │ wraps    │   │  built into Podman CLI       │   │
│  │ (limactl)│   │ Lima     │   │                              │   │
│  └────┬─────┘   └────┬─────┘   └──────────────┬───────────────┘   │
│       │              │                         │                   │
│       └──────────────┘                         │                   │
│              │                                 │                   │
│    creates Linux VMs                  creates Linux VMs            │
│              │                                 │                   │
│   ┌──────────▼──────────┐         ┌────────────▼────────────┐     │
│   │   Linux VM          │         │   Linux VM              │     │
│   │                     │         │                         │     │
│   │  You choose what    │         │  Podman is pre-installed│     │
│   │  runs inside:       │         │  automatically          │     │
│   │  • Podman           │         │                         │     │
│   │  • containerd/nerdctl│        │  ┌───────────────────┐  │     │
│   │  • Docker           │         │  │  Podman (runtime) │  │     │
│   │  • nothing          │         │  └───────────────────┘  │     │
│   └─────────────────────┘         └─────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
```

**Key insight:** Lima and Colima are VM managers — they do not run containers
themselves. The container runtime (Podman, nerdctl, Docker) is software you
install *inside* the Linux VM they create.

**Colima wraps Lima:**

```
you call:   colima start --runtime containerd
              │
              └──► limactl create ...   (Lima creates the VM)
              └──► installs containerd + nerdctl inside the VM
              └──► configures nerdctl so you can call it directly on macOS
```

Colima is a convenience layer: it automates the "create Lima VM, install runtime,
configure CLI" workflow so you don't have to do it manually.

### The naming problem: "podman" means three different things

Each tool has three layers — VM manager, container runtime, CLI — but only Podman
reuses the same name at every layer:

```
Layer            Podman Machine          Lima (direct)           Colima
───────────────────────────────────────────────────────────────────────────────
VM manager       podman machine          limactl                 colima
                 (manages the VM)        (manages the VM)        (wraps limactl)
                      │                       │                       │
                      ▼                       ▼                       ▼
Container        podman                  containerd              containerd
runtime          (inside the VM)         (inside the VM)         (inside the VM)
                      │                       │                       │
                      ▼                       ▼                       ▼
CLI on host      podman run              nerdctl run             nerdctl run
                 (proxied to VM)         (via limactl shell)     (direct, socket)
```

With Podman, `podman` on the host CLI, `podman` as the in-VM runtime, and
`podman machine` as the VM manager all share the same binary name. Lima and
Colima use distinct names at each layer (`limactl`, `containerd`, `nerdctl`).

---

## 2. The VM type determines what architectures you can run

Every VM has one `vmType` — it cannot be changed after creation:

| vmType | What it does | ARM64 | x86_64 | x86-64-v2 (SSE4.2) |
|--------|-------------|-------|--------|---------------------|
| `vz` (Apple Hypervisor) | Runs native arm64 VM; uses Rosetta 2 to translate x86_64 binaries | ✅ native | ✅ via Rosetta 2 | ⚠️ incomplete |
| `qemu` (x86_64) | Emulates a full x86_64 CPU | ❌ | ✅ native | ✅ complete |

DB2 specifically probes for x86-64-v2 features at startup. Rosetta 2 does not
fully satisfy this requirement. That is why DB2 fails under VZ/Rosetta but
works reliably under QEMU.

---

## 3. Why you need two VMs

You cannot have both Rosetta 2 speed and full QEMU emulation in the same VM.
To run DB2 reliably *and* keep ARM-native speed for Postgres/MySQL/CockroachDB,
you need two separate VMs:

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Apple Silicon Mac (arm64)                         │
│                                                                      │
│  VM 1: VZ + Rosetta 2  (fast)          VM 2: QEMU x86_64 (complete) │
│  ┌──────────────────────────────┐      ┌────────────────────────┐   │
│  │  Linux VM (arm64)            │      │  Linux VM (x86_64)     │   │
│  │  Podman or nerdctl inside    │      │  Podman inside         │   │
│  │                              │      │                        │   │
│  │  pg18      [arm64 native]    │      │  db2ce  [x86_64]       │   │
│  │  crdb24    [arm64 native]    │      │         ↑              │   │
│  │  mysql8    [arm64 native]    │      │  x86-64-v2 satisfied   │   │
│  │  sqlserver [amd64 →Rosetta]  │      │  SSE4.2, POPCNT ✅     │   │
│  │  oracle    [amd64 →Rosetta]  │      │                        │   │
│  └──────────────────────────────┘      └────────────────────────┘   │
│                                                                      │
│  ~1.5–2× native speed for x86_64       ~5–10× slower, but correct  │
└──────────────────────────────────────────────────────────────────────┘
```

There is no shortcut. One VM, one vmType.

---

## 4. How each tool provides the two VMs

### Option A: Podman Machine (Podman 5.x on macOS)

```
podman-machine-default  (applehv = VZ + Rosetta)   → pg, crdb, mysql, sqlserver, oracle
Lima db2 VM             (QEMU x86_64)              → db2
```

**Problem:** Podman 5.x on macOS removed `--vmtype`/`--arch` flags. You cannot
create a QEMU Podman machine anymore. The only QEMU option is Lima directly.

### Option B: Lima directly

```
Lima VM: statschema  (vmType: vz,   nerdctl inside)   → pg, crdb, mysql, sqlserver, oracle
Lima VM: db2         (vmType: qemu, Podman inside)     → db2
```

You call containers via:
```bash
limactl shell statschema -- nerdctl run ...   # ARM/Rosetta VM
limactl shell db2        -- sudo podman run ...  # QEMU VM (db2lima)
```

### Option C: Colima (two named instances)

```
colima start arm  --vmtype vz   --arch aarch64 --vz-rosetta --runtime containerd
colima start qemu --vmtype qemu --arch x86_64              --runtime containerd
```

Internally, each `colima start` creates a separate Lima VM. You route commands
by passing the containerd socket address:

```bash
# Targeting the arm instance:
nerdctl --address ~/.colima/arm/containerd.sock run ...

# Targeting the qemu instance:
nerdctl --address ~/.colima/qemu/containerd.sock run ...
```

---

## 5. Code complexity comparison

The three approaches map to the three shell libraries in this repo:

| File | VM backend | Container command | DB2 QEMU command | Extra lines vs _common.sh |
|------|-----------|-------------------|-----------------|--------------------------|
| `_common.sh` | Podman Machine (default) | `podman run` | `limactl shell db2 -- sudo podman` | baseline |
| `_common_lima.sh` | Lima `statschema` VM | `limactl shell statschema -- nerdctl run` | `limactl shell db2 -- sudo podman` | +~10 (ensure_lima helper) |
| `_common_colima.sh` | Colima (single instance) | `nerdctl run` | `limactl shell db2 -- sudo podman` | +~40 (ensure_colima, _ensure_nerdctl) |

**Winner for simplicity:** `_common.sh` — Podman Machine manages the VM
transparently; `podman run` just works. Lima is only invoked for DB2.

**Winner for explicit control:** `_common_lima.sh` — you own the VM
configuration (arch, memory, vmType) and there is no hidden layer.

**Most portable across macOS/Linux:** `_common_colima.sh` — Colima works
on both platforms with the same syntax.

---

## 6. The dependency chain in full

```
_common.sh (current setup)
└── podman run  ──────────────────► Podman Machine (applehv/VZ + Rosetta)
│                                       └── Linux VM (arm64)
│                                             └── Podman
│                                                   └── containers
└── limactl shell db2 -- podman ──► Lima VM: db2 (QEMU x86_64)
                                        └── Linux VM (x86_64)
                                              └── Podman
                                                    └── db2ce

_common_lima.sh
└── limactl shell statschema -- nerdctl ──► Lima VM: statschema (VZ + Rosetta)
│                                               └── Linux VM (arm64)
│                                                     └── containerd / nerdctl
│                                                           └── containers
└── limactl shell db2 -- podman ──────────► Lima VM: db2 (QEMU x86_64)
                                                └── Linux VM (x86_64)
                                                      └── Podman
                                                            └── db2ce

_common_colima.sh
└── nerdctl run ──────────────────────────► Colima (wraps Lima VZ + Rosetta)
│                                               └── Lima VM (arm64)
│                                                     └── containerd / nerdctl
│                                                           └── containers
└── limactl shell db2 -- podman ──────────► Lima VM: db2 (QEMU x86_64)
                                                └── Linux VM (x86_64)
                                                      └── Podman
                                                            └── db2ce
```

All three setups use **exactly two VMs** for full database coverage on Apple Silicon.
The difference is only in who manages the first VM and how you address it.
