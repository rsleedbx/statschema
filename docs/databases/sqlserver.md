# SQL Server — local setup

← [All databases](../local-databases.md)

**Method:** Lima VM + QEMU, x86_64 · **Port:** 14330 · **Driver:** `pymssql`

SQL Server for Linux has no linux/arm64 image ([mssql-docker #864](https://github.com/microsoft/mssql-docker/issues/864)).  This repo runs it in a Lima VM under QEMU (linux/amd64).

## Prerequisites

```bash
brew tap microsoft/mssql-release
brew install lima pwgen mssql-tools
```

`mssql-tools` provides `sqlcmd` for command-line access.  Optionally install [Azure Data Studio](https://azure.microsoft.com/en-us/products/data-studio) for a GUI client.

## Create the Lima VM

```bash
# Check for an existing VM first
limactl list

# Create fresh from the checked-in config
limactl start --name=sqlserver config/lima/sqlserver.yaml
```

Lima will:
1. Download an Ubuntu 20.04 x86_64 cloud image (~500 MB, first run only)
2. Boot the VM under QEMU (~1–2 min)
3. Install SQL Server 2022
4. Generate a random SA password and log it to cloud-init output
5. Wait until SQL Server is listening on port 14330

> Host port 14330; configured in `config/lima/sqlserver.yaml`.

## Start SQL Server after VM boot

SQL Server does **not** auto-start when the Lima VM boots:

```bash
limactl shell sqlserver22 -- sudo systemctl start mssql-server

# Verify it is listening
nc -zv 127.0.0.1 14330
```

## Find the SA password

```bash
limactl shell sqlserver22 -- grep "SQL Server sa password is" \
    /var/log/cloud-init-output.log | tail -1

# Export for the session
export SQLSERVER_PASS="<password from above>"
export SQLSERVER_PORT=14330
```

## Connect from macOS

```bash
sqlcmd -S 127.0.0.1,14330 -U sa -P "$SQLSERVER_PASS" -C
```

GUI client connection:

| Field | Value |
|-------|-------|
| Server | `127.0.0.1` |
| Port | `14330` |
| Authentication | SQL Login |
| User | `sa` |
| Password | _(from cloud-init-output.log)_ |

## Run the live tests

```bash
SQLSERVER_PASS="$SQLSERVER_PASS" \
SQLSERVER_PORT=14330 \
.venv_test/bin/python -m pytest tests/test_live_sqlserver.py -v
```

Expected result: **14 passed**.  Tests auto-skip when `SQLSERVER_PASS` is not set.

## Common VM operations

```bash
limactl stop  sqlserver22    # shut down (preserves data)
limactl start sqlserver22    # restart
limactl shell sqlserver22    # shell inside the VM
limactl delete sqlserver22   # destroy completely
```

Check SQL Server logs:

```bash
limactl shell sqlserver22 -- sudo tail -f /var/opt/mssql/log/errorlog
```

## Troubleshooting

**Port 14330 closed (Connection refused)**
```bash
limactl shell sqlserver22 -- sudo systemctl status mssql-server
# If inactive: sudo systemctl start mssql-server
```

**`Login failed for user 'sa'`**
Multiple provision runs create multiple log entries.  Use `tail -1` to get the most recent password.

**Lima stuck at "Waiting for the optional requirement"**
SQL Server is still initialising.  Check progress:
```bash
limactl shell sqlserver22 -- sudo tail -20 /var/opt/mssql/log/errorlog
# Ready when you see: Recovery is complete.
```

## Sample databases

- [AdventureWorks](adventureworks.md)
- [Chinook](chinook.md)
