# Why does Podman require a machine VM on macOS?

## Short answer

Linux containers require a Linux kernel.  macOS does not have one.  Podman
transparently runs a lightweight Linux VM (the "Podman machine") so that
containers have a kernel to run on.  You interact with `podman` on the macOS
command line exactly as you would on Linux — the VM is invisible during normal
use.

## What `podman machine` is

`podman machine` manages a minimal Linux VM (backed by QEMU or Apple
Hypervisor Framework) that hosts the container runtime.  Podman on macOS
routes all container commands through this VM via a Unix socket.

```
macOS host
  └─ podman CLI  ──socket──►  Podman machine (Linux VM)
                                  └─ container runtime (crun / runc)
                                        └─ postgres:16 container
                                        └─ mysql:8.4 container
```

## One-time setup

```bash
brew install podman          # install the CLI and supporting tools
podman machine init          # create the Linux VM (~500 MB download, one time)
podman machine start         # boot the VM (stays running in background)
podman info                  # verify everything is working
```

The machine persists across reboots.  Start it automatically in your shell:

```bash
# Add to ~/.zshrc
podman machine start 2>/dev/null || true
```

## Why Podman and not Docker Desktop?

| | Podman | Docker Desktop |
|---|---|---|
| Licence | Free, open-source (Apache 2.0) | Paid for commercial use (>250 employees or >$10 M revenue) |
| Daemon | Daemonless (rootless by default) | Requires a background daemon |
| CLI compatibility | `podman` is a drop-in replacement for `docker` | — |
| macOS VM backend | QEMU or Apple HVF | Apple HVF |
| Container registries | Docker Hub, GHCR, any OCI registry | Same |

Docker Desktop requires a commercial subscription for organisations above a
certain size.  Podman is free for all use cases and its CLI is a drop-in
replacement (`alias docker=podman` works for all commands in this project).

## What Podman runs in this project

| Container | Image | Port | Architecture |
|-----------|-------|------|-------------|
| `postgres-test` / `pg14` / `pg16` | `docker.io/library/postgres:14` / `postgres:16` | 5414 / 5416 | Native ARM64 |
| `mysql-test` / `mysql8` | `docker.io/library/mysql:8.4` | 3384 | Native ARM64 |
| `mysql57` | `docker.io/library/mysql:5.7` | 3357 | x86_64 (emulated via Rosetta 2 inside VM) |

**Oracle XE and SQL Server are not run via `podman` directly on macOS.**
They require a separate **Lima VM** (which uses full QEMU x86_64 emulation)
because there are no ARM64 builds of either database.  See
[FAQ: What is gvenzl/oracle-xe?](17-what-is-gvenzl-oracle-xe.md) and
[`docs/local-databases.md`](../local-databases.md) for details.

> Within the Lima Oracle VM, Podman is also used to run the Oracle XE container
> — but that Podman instance is inside the Linux VM, not the macOS Podman machine.

## Quick-start commands

```bash
# Check VM status
podman machine list

# Start all test containers (after machine is running)
podman run -d --name pg16   -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb \
  -p 5416:5432 docker.io/library/postgres:16
podman run -d --name mysql8 -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=testdb \
  -p 3384:3306 docker.io/library/mysql:8.4

# Stop all (preserves data volumes)
podman stop pg16 mysql8

# Remove all (destroys data)
podman rm -f pg16 mysql8

# Stop the Podman machine (optional — frees ~200 MB RAM)
podman machine stop
```

## Troubleshooting

**`Cannot connect to Podman. Please verify that Podman is installed and running.`**
```bash
podman machine start
```

**`Error: no such image` or slow pull**
Docker Hub rate-limits anonymous pulls to ~100/6 h per IP.  If you hit this:
```bash
podman login docker.io   # log in with a free Docker Hub account
```

**MySQL 5.7 fails to start on Apple Silicon**
MySQL 5.7 has no ARM64 image.  Add `--platform linux/amd64` to run via Rosetta 2
emulation inside the Podman VM:
```bash
podman run -d --name mysql57 --platform linux/amd64 \
  -e MYSQL_ROOT_PASSWORD=testpass -e MYSQL_DATABASE=testdb \
  -p 3357:3306 docker.io/library/mysql:5.7
```

## Related

- [`docs/local-databases.md`](../local-databases.md) — full setup for all databases
- [FAQ: What is `gvenzl/oracle-xe`?](17-what-is-gvenzl-oracle-xe.md)
- [Podman documentation](https://docs.podman.io)
- [Podman machine documentation](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)
