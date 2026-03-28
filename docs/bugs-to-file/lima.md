# Bugs to file — Lima (limactl)

---

## Bug 1 — Changing `cpus:` or `memory:` in config YAML has no effect on existing VMs

**Product**: Lima v0.x (limactl)
**Where to file**: https://github.com/lima-vm/lima/issues
**Severity**: Medium — silent misconfiguration; developer expects the edit to take effect

### Description

When a Lima VM already exists (created with `limactl start --name=<vm>
config.yaml`), subsequent edits to the config YAML file and a `limactl start
<vm>` do **not** apply the new `cpus:` or `memory:` values.  Lima silently
reuses the instance config stored at `~/.lima/<vm>/lima.yaml` from the original
creation, ignoring the edited source file.

There is no warning, no error, and no indication in `limactl start` output that
the requested config was not applied.

### Steps to reproduce

```bash
# Create a VM with 2 CPUs
limactl start --name=myvm config/lima/mydb.yaml   # cpus: 2 in YAML

# Edit the source YAML to 4 CPUs
sed -i 's/cpus: 2/cpus: 4/' config/lima/mydb.yaml

# Restart — expects 4 CPUs
limactl stop myvm
limactl start myvm

# Actual: still 2 CPUs
limactl shell myvm -- nproc   # prints 2
```

### Expected behaviour

Either:
- `limactl start <vm>` detects the source config has changed and applies
  the new settings (or warns that a destructive recreate is required), or
- the CLI documentation prominently states that resource changes require
  `limactl delete <vm>` + recreate.

### Actual behaviour

The VM boots with the original resource allocation.  `limactl start` prints no
warning.  The developer assumes the new config is active.

### Workaround

Edit `~/.lima/<vm>/lima.yaml` directly before `limactl start`, **or** delete
and recreate the VM from the config:

```bash
limactl delete myvm
limactl start --name=myvm config/lima/mydb.yaml
```

**Warning**: recreating a VM destroys all data inside it.  Databases that run
inside Podman containers within the VM (e.g. DB2) will lose all container state
and must be re-initialized from scratch.  This can take 5–10 minutes for DB2
due to first-boot setup.

### Impact in our codebase

Attempted to increase DB2 Lima VM from 2 to 4 vCPUs for performance testing.
The silent config reuse caused the VM to boot with the old CPU count.
Direct edit of `~/.lima/db2/lima.yaml` triggered container state loss and a
failed DB2 first-boot initialization (`DBI1264E`).  See
`config/lima/db2.yaml` and `docs/testing.md`.

---

## Bug 2 — No diff or dry-run mode to preview config changes before `limactl start`

**Product**: Lima (limactl)
**Where to file**: https://github.com/lima-vm/lima/issues
**Severity**: Low — developer ergonomics / feature request

### Description

There is no `limactl config diff` or `limactl start --dry-run` command to show
which settings would change (or be ignored) before committing to a potentially
destructive VM recreation.  Combined with Bug 1, this means a developer has no
safe way to preview whether their YAML edits will take effect.

### Suggested fix

Add a `limactl start --dry-run` flag that prints:
- Which settings differ between the source YAML and the running instance config
- Whether applying them requires a VM recreate or can be applied on next start
