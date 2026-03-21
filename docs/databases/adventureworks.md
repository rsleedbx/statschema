# AdventureWorks — application-level testing (SQL Server)

← [All databases](../local-databases.md)

**Requires:** [SQL Server](sqlserver.md) (`sqlserver22` Lima VM running on port 14330)

Microsoft's canonical sample databases, provided as `.bak` restore files:

| Database | Tables | Description |
|----------|--------|-------------|
| **AdventureWorksLT2022** | 12 | Lightweight — retail customer/product/sales. Quick to load (~8 MB). |
| **AdventureWorks2022** | 71 | Full — 6 schemas: HumanResources, Person, Production, Purchasing, Sales, dbo. Exercises every major SQL Server type. |

Full database types include: user-defined types (`Name`, `Flag`, `Phone`, `AccountNumber`), `MONEY`, `UNIQUEIDENTIFIER`, `ROWVERSION`/`timestamp`, `xml`, `hierarchyid`, `geography`.

## One-time setup

```bash
# 1 – Download .bak files
curl -sL "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksLT2022.bak" \
     -o /tmp/AdventureWorksLT2022.bak
curl -sL "https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks2022.bak" \
     -o /tmp/AdventureWorks2022.bak   # ~200 MB

# 2 – Copy into Lima VM
limactl copy /tmp/AdventureWorksLT2022.bak sqlserver22:/tmp/
limactl copy /tmp/AdventureWorks2022.bak   sqlserver22:/tmp/

# 3 – Move to SQL Server-accessible path (inside VM)
limactl shell sqlserver22 -- sudo bash -c "
  mkdir -p /var/opt/mssql/backup
  cp /tmp/AdventureWorks*.bak /var/opt/mssql/backup/
  chown -R mssql:mssql /var/opt/mssql/backup
"

# 4 – Restore both databases
source .env
sqlcmd -S "127.0.0.1,${SQLSERVER_PORT:-14330}" -U sa -P "$SQLSERVER_PASS" -C -Q "
  RESTORE DATABASE [AdventureWorksLT2022]
  FROM DISK = '/var/opt/mssql/backup/AdventureWorksLT2022.bak'
  WITH MOVE 'AdventureWorksLT2022_Data' TO '/var/opt/mssql/data/AdventureWorksLT2022.mdf',
       MOVE 'AdventureWorksLT2022_Log'  TO '/var/opt/mssql/data/AdventureWorksLT2022_log.ldf',
       REPLACE;
  RESTORE DATABASE [AdventureWorks2022]
  FROM DISK = '/var/opt/mssql/backup/AdventureWorks2022.bak'
  WITH MOVE 'AdventureWorks2022'     TO '/var/opt/mssql/data/AdventureWorks2022.mdf',
       MOVE 'AdventureWorks2022_log' TO '/var/opt/mssql/data/AdventureWorks2022_log.ldf',
       REPLACE;
"
```

## Run the tests

```bash
make test-live-adventureworks
```

Expected output: **34 passed** (14 for AWLT + 20 for full AW2022).

## Exotic type handling

SQL Server types with no direct canonical equivalent are remapped to `NVARCHAR(MAX)` during DDL extraction:

| SQL Server type | Canonical fallback |
|----------------|---------------------|
| `hierarchyid` | `string` (via NVARCHAR MAX) |
| `geography` | `string` |
| `xml` | `string` |
| `sql_variant` | `string` |
| `timestamp` (rowversion) | `string` |

User-defined types are resolved to their base type via `sys.types`.
