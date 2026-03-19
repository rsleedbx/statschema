# What is `gvenzl/oracle-xe` and why do we use it?

## What it is

`gvenzl/oracle-xe` is a community-maintained container image for
**Oracle Database Express Edition (XE)**, published on Docker Hub at
[hub.docker.com/r/gvenzl/oracle-xe](https://hub.docker.com/r/gvenzl/oracle-xe).

It is created and maintained by **Gerald Venzl**, a Principal Product Manager
on the Oracle Database team at Oracle Corporation.  Although it is a personal
project rather than an official Oracle product, it has widespread adoption
(10 M+ Docker Hub pulls) and is the de-facto standard image for running
Oracle XE in development and CI environments.

Source code and full documentation:
[github.com/gvenzl/oci-oracle-xe](https://github.com/gvenzl/oci-oracle-xe)

## Available tags

| Tag | Oracle version | Approximate image size | Notes |
|-----|---------------|------------------------|-------|
| `21-slim` | Oracle 21c XE | ~800 MB | Removes EM Express UI and extra locale data; **recommended for testing** |
| `21` | Oracle 21c XE | ~2 GB | Full install |
| `18-slim` | Oracle 18c XE | ~650 MB | |
| `11-slim` | Oracle 11g XE | ~600 MB | Oldest supported version |

We use **`21-slim`** in `config/lima/oracle.yaml`.

## Why we use it instead of the official Oracle image

| | `gvenzl/oracle-xe:21-slim` | `container-registry.oracle.com/database/express:21.3.0-xe` |
|---|---|---|
| Registry | Docker Hub (public) | Oracle Container Registry |
| Authentication | None — anonymous pull | Oracle account + licence click-through per machine |
| CI/CD friendly | Yes | No — interactive login required |
| Image size | ~800 MB | ~2.5 GB |
| Maintained by | Oracle employee (personal) | Oracle Corp |
| Pull command | `podman pull gvenzl/oracle-xe:21-slim` | `podman login container-registry.oracle.com` first |

The primary reasons for choosing it:

1. **No account or licence acceptance required** — works out of the box in any
   terminal, CI runner, or Lima VM provisioning script without human interaction.
2. **Much smaller** — the `slim` variant is ~3× smaller than the official image,
   which matters when downloading inside a QEMU VM over a typical broadband connection.
3. **Well maintained** — Gerald keeps pace with Oracle XE releases and patches.

## Environment variables

The image accepts a small set of environment variables at container start:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORACLE_PASSWORD` | *(required)* | Password for `SYS`, `SYSTEM`, and `PDBADMIN` |
| `ORACLE_DATABASE` | `XEPDB1` | Name of the pluggable database created on first boot |
| `APP_USER` | *(unset)* | Optional: create a non-system application user |
| `APP_USER_PASSWORD` | *(unset)* | Password for `APP_USER` |

In `config/lima/oracle.yaml` we set `ORACLE_PASSWORD=oracle` (suitable only
for local development — never use in production).

## Connection details (with this repo's default config)

| Parameter | Value |
|-----------|-------|
| Host | `127.0.0.1` |
| Port | `1521` |
| Service name | `XE` |
| User | `system` |
| Password | `oracle` |

```python
import oracledb
conn = oracledb.connect(user="system", password="oracle",
                        dsn="127.0.0.1:1521/XE")
```

## First-boot initialisation time

On first start, Oracle XE initialises its data files.  This takes **3–5 minutes**
inside the QEMU x86_64 Lima VM.  Poll for readiness:

```bash
until limactl shell oracle -- podman logs oracle-xe 2>/dev/null \
      | grep -q "DATABASE IS READY TO USE"; do
  echo "[$(date +%H:%M:%S)] waiting…"
  sleep 15
done
echo "Oracle XE ready!"
```

Subsequent starts (after `limactl stop oracle` + `limactl start oracle`) take
30–60 seconds because data files already exist.

## Related

- [`config/lima/oracle.yaml`](../../config/lima/oracle.yaml) — Lima VM config
- [`docs/local-databases.md`](../local-databases.md) — full Oracle XE setup guide
- [`tests/test_live_oracle.py`](../../tests/test_live_oracle.py) — live test suite
- [FAQ: Why does Podman require a machine VM on macOS?](18-podman-machine-on-macos.md)
