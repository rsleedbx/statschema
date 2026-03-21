# Chinook — application-level testing (SQL Server)

← [All databases](../local-databases.md)

**Requires:** [SQL Server](sqlserver.md) (`sqlserver22` Lima VM running on port 14330)

[Chinook](https://github.com/lerocha/chinook-database) — 11 tables.  Types: `NVARCHAR`, `INTEGER`, `DECIMAL`, `DATETIME`, `NUMERIC`, composite PKs, FK chains.

## One-time setup

```bash
# Download and load the Chinook T-SQL script into the SQL Server 22 Lima VM
curl -sL "https://raw.githubusercontent.com/lerocha/chinook-database/master/\
ChinookDatabase/DataSources/Chinook_SqlServer.sql" -o /tmp/chinook_sqlserver.sql

source .env
sqlcmd -S "127.0.0.1,${SQLSERVER_PORT:-14330}" -U sa -P "$SQLSERVER_PASS" \
       -C -i /tmp/chinook_sqlserver.sql
```

## Run the tests

```bash
make test-live-chinook
```

Expected output: **16 passed**.
