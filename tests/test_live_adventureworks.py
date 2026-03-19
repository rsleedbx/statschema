"""
Live AdventureWorks integration tests — runs against two Microsoft sample databases
restored into the SQL Server 2022 Lima VM.

AdventureWorksLT2022 (Lightweight)
-----------------------------------
12 tables across two schemas (``dbo``, ``SalesLT``).  A compact retail sample
covering customers, addresses, products, and sales orders.  Good baseline for
SQL Server types and cross-schema FK relationships.

AdventureWorks2022 (Full)
--------------------------
71 tables across six schemas: ``dbo``, ``HumanResources``, ``Person``,
``Production``, ``Purchasing``, ``Sales``.  The canonical SQL Server sample
database.  Exercises every common SQL Server-specific type:

* User-defined types (``Name`` → ``NVARCHAR(50)``, ``Flag`` → ``BIT``, ``Phone`` → ``NVARCHAR(25)``, etc.)
* ``timestamp`` / ``ROWVERSION``
* ``xml``, ``hierarchyid``, ``geography``  (remapped to ``NVARCHAR(MAX)`` in canonical model)
* ``sql_variant``
* ``MONEY``, ``SMALLMONEY``
* ``UNIQUEIDENTIFIER``
* Composite primary keys and multi-schema foreign keys

Prerequisites
-------------
Download and restore both databases from the Microsoft sample releases:

    # Download (run on macOS host)
    curl -sL https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorksLT2022.bak \\
         -o /tmp/AdventureWorksLT2022.bak
    curl -sL https://github.com/Microsoft/sql-server-samples/releases/download/adventureworks/AdventureWorks2022.bak \\
         -o /tmp/AdventureWorks2022.bak

    # Copy into Lima VM
    limactl copy /tmp/AdventureWorksLT2022.bak sqlserver22:/tmp/
    limactl copy /tmp/AdventureWorks2022.bak   sqlserver22:/tmp/

    # Move to SQL Server-readable location inside VM
    limactl shell sqlserver22 -- sudo bash -c "
      mkdir -p /var/opt/mssql/backup
      cp /tmp/AdventureWorks*.bak /var/opt/mssql/backup/
      chown -R mssql:mssql /var/opt/mssql/backup
    "

    # Restore (run from macOS host, using sqlcmd)
    source .env
    sqlcmd -S 127.0.0.1,${SQLSERVER_PORT} -U sa -P "$SQLSERVER_PASS" -C -Q "
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

Environment variables (defaults match the Lima VM setup):

    SQLSERVER_HOST   default: 127.0.0.1
    SQLSERVER_PORT   default: 14330
    SQLSERVER_PASS   (required; see .env)

Skip behaviour
--------------
All tests skip when SQL Server is unreachable, ``SQLSERVER_PASS`` is unset, or
pymssql is not installed — ``make test`` always completes cleanly.
"""

from __future__ import annotations

import os

import pytest

from src.statschema import emit_ddl, load_canonical, parse_ddl
from src.statschema.model import CanonicalTableSchema, expand_table_instances
from src.statschema.schema_io import dump_schema as dump_canonical

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_HOST = os.environ.get("SQLSERVER_HOST", "127.0.0.1")
_PORT = int(os.environ.get("SQLSERVER_PORT", "14330"))
_PASS = os.environ.get("SQLSERVER_PASS", "")

# ---------------------------------------------------------------------------
# Helpers shared by both databases
# ---------------------------------------------------------------------------

def _get_conn(database: str):
    if not _PASS:
        pytest.skip("SQLSERVER_PASS not set")
    pymssql = pytest.importorskip("pymssql")
    try:
        conn = pymssql.connect(
            server=_HOST, port=_PORT,
            user="sa", password=_PASS,
            database=database,
        )
        conn.autocommit(True)
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to {database} on {_HOST}:{_PORT}: {exc}")


def _build_udt_map(conn) -> dict[str, str]:
    """Return a dict mapping lower-case UDT name → resolved SQL type string."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.name, bt.name AS base_type, t.max_length, t.precision, t.scale
        FROM sys.types t
        JOIN sys.types bt ON t.system_type_id = bt.user_type_id
        WHERE t.is_user_defined = 1
        """
    )
    udt_map: dict[str, str] = {}
    for udt_name, base_type, max_len, prec, scale in cur.fetchall():
        base = base_type.upper()
        if "nvarchar" in base_type.lower() and max_len:
            base = f"NVARCHAR({max_len // 2})"
        elif "varchar" in base_type.lower() and max_len:
            base = f"VARCHAR({max_len})"
        elif "char" in base_type.lower() and max_len:
            base = f"NCHAR({max_len // 2})"
        udt_map[udt_name.lower()] = base
    return udt_map


# Exotic SQL Server types that have no direct canonical equivalent — fall back
# to NVARCHAR(MAX) so the parser still produces a usable schema.
_EXOTIC_TYPES = frozenset({
    "HIERARCHYID", "GEOGRAPHY", "GEOMETRY", "XML",
    "SQL_VARIANT", "TIMESTAMP", "IMAGE",
})


def _all_tables(conn) -> list[tuple[str, str]]:
    """Return list of (schema, table_name) sorted by schema then name."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT SCHEMA_NAME(schema_id), name
        FROM sys.tables
        ORDER BY SCHEMA_NAME(schema_id), name
        """
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def _get_ddl(conn, schema: str, table: str, udt_map: dict[str, str]) -> str:
    """Build a CREATE TABLE DDL string, resolving UDTs and exotic types."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.COLUMN_NAME, c.DATA_TYPE,
               c.CHARACTER_MAXIMUM_LENGTH,
               c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
               c.IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s
        ORDER BY c.ORDINAL_POSITION
        """,
        (schema, table),
    )
    cols = cur.fetchall()

    # Primary key columns
    cur.execute(
        """
        SELECT ku.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
          ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
        WHERE tc.TABLE_SCHEMA = %s AND tc.TABLE_NAME = %s
          AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY ku.ORDINAL_POSITION
        """,
        (schema, table),
    )
    pks = [r[0] for r in cur.fetchall()]
    pk_set = set(pks)

    parts = []
    for name, dtype, maxlen, prec, scale, nullable in cols:
        resolved = udt_map.get(dtype.lower(), dtype.upper())
        if resolved == dtype.upper():
            if maxlen == -1:
                resolved += "(MAX)"
            elif maxlen:
                resolved += f"({maxlen})"
            elif prec and dtype.upper() in ("NUMERIC", "DECIMAL"):
                resolved += f"({prec},{scale or 0})"

        # Remap exotic types to a parseable fallback
        if resolved.upper().split("(")[0] in _EXOTIC_TYPES:
            resolved = "NVARCHAR(MAX)"

        nn = " NOT NULL" if nullable == "NO" else ""
        parts.append(f"  [{name}] {resolved}{nn}")

    if len(pks) == 1:
        parts = [
            p + " PRIMARY KEY" if p.strip().startswith(f"[{pks[0]}]") else p
            for p in parts
        ]
    elif pks:
        pk_list = ", ".join(f"[{p}]" for p in pks)
        parts.append(f"  CONSTRAINT PK_{table} PRIMARY KEY ({pk_list})")

    return f"CREATE TABLE [{schema}].[{table}] (\n" + ",\n".join(parts) + "\n)"


# ===========================================================================
# AdventureWorksLT2022  (lightweight — 12 tables)
# ===========================================================================

class TestAWLTConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn("AdventureWorksLT2022")
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) == 12, f"Expected 12 AWLT tables, got {len(tables)}"

    def test_awlt_schemas_and_tables_present(self):
        conn = _get_conn("AdventureWorksLT2022")
        tables_by_schema: dict[str, set[str]] = {}
        for schema, tbl in _all_tables(conn):
            tables_by_schema.setdefault(schema, set()).add(tbl)
        conn.close()

        assert "SalesLT" in tables_by_schema
        assert "dbo" in tables_by_schema
        for expected in ("Address", "Customer", "Product", "SalesOrderHeader", "SalesOrderDetail"):
            assert expected in tables_by_schema["SalesLT"], f"Missing SalesLT.{expected}"


class TestAWLTParse:
    def test_all_awlt_tables_parse_without_error(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        failures: list[tuple[str, str]] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            try:
                result = parse_ddl(ddl, dialect="tsql")
                assert result, f"parse_ddl returned empty list for {schema}.{tbl}"
            except Exception as exc:
                failures.append((f"{schema}.{tbl}", str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} AWLT tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_customer_table_columns(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "Customer", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("CustomerID", "FirstName", "LastName", "EmailAddress"):
            assert expected in col_names, f"Column '{expected}' missing from Customer"

        pk = next((c for c in t.columns if c.primary_key), None)
        assert pk is not None, "Customer has no primary key column"
        assert pk.name == "CustomerID"

    def test_product_table_columns(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "Product", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("ProductID", "Name", "ListPrice", "StandardCost"):
            assert expected in col_names, f"Column '{expected}' missing from Product"

        price_col = next((c for c in t.columns if c.name == "ListPrice"), None)
        assert price_col is not None
        assert price_col.type in ("decimal", "long", "integer"), (
            f"Expected numeric type for ListPrice, got {price_col.type}"
        )

    def test_sales_order_detail_columns(self):
        """SalesOrderDetail has UNIQUEIDENTIFIER, DECIMAL, and composite FK."""
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "SalesOrderDetail", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("SalesOrderID", "SalesOrderDetailID", "OrderQty", "UnitPrice"):
            assert expected in col_names, f"Column '{expected}' missing from SalesOrderDetail"


class TestAWLTEmit:
    def test_all_awlt_tables_emit_without_error(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        failures: list[tuple[str, str]] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            try:
                parsed = parse_ddl(ddl, dialect="tsql")
                if parsed:
                    emit_ddl(parsed[0], dialect="sqlserver")
            except Exception as exc:
                failures.append((f"{schema}.{tbl}", str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} AWLT tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_customer(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl1 = _get_ddl(conn, "SalesLT", "Customer", udt_map)
        conn.close()

        t1   = parse_ddl(ddl1, dialect="tsql")[0]
        ddl2 = emit_ddl(t1, dialect="sqlserver", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="tsql")[0]
        ddl3 = emit_ddl(t2, dialect="sqlserver", if_not_exists=False)
        assert ddl2 == ddl3, "Customer double round-trip not idempotent"

    def test_cross_dialect_product_to_postgresql(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "Product", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        pg_ddl = emit_ddl(t, dialect="postgresql")
        assert "CREATE TABLE" in pg_ddl
        assert "ListPrice" in pg_ddl

    def test_cross_dialect_address_to_mysql(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "Address", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        mysql_ddl = emit_ddl(t, dialect="mysql")
        assert "CREATE TABLE" in mysql_ddl
        assert "City" in mysql_ddl

    def test_cross_dialect_sales_order_to_oracle(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "SalesOrderHeader", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        ora_ddl = emit_ddl(t, dialect="oracle")
        assert "CREATE TABLE" in ora_ddl
        assert "TotalDue" in ora_ddl


class TestAWLTMultiInstance:
    def test_sales_order_detail_shard_expansion(self):
        """Simulate 4-shard SalesOrderDetail (e.g. per quarter)."""
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "SalesOrderDetail", udt_map)
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=4,
            instance_suffix_format="_Q{:01d}",
        )
        expanded = expand_table_instances([base])
        assert len(expanded) == 4
        assert [t.name for t in expanded] == [
            "SalesOrderDetail_Q1",
            "SalesOrderDetail_Q2",
            "SalesOrderDetail_Q3",
            "SalesOrderDetail_Q4",
        ]

    def test_product_with_aliases(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "SalesLT", "Product", udt_map)
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["Product_Catalog", "Product_Archive"],
        )
        expanded = expand_table_instances([base])
        assert len(expanded) == 3
        names = {t.name for t in expanded}
        assert "Product" in names
        assert "Product_Catalog" in names

    def test_awlt_full_yaml_roundtrip(self):
        conn = _get_conn("AdventureWorksLT2022")
        udt_map = _build_udt_map(conn)
        parsed: list[CanonicalTableSchema] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            result = parse_ddl(ddl, dialect="tsql")
            if result:
                parsed.append(result[0])
        conn.close()

        yaml_str = dump_canonical(parsed)
        assert "SalesOrderHeader" in yaml_str
        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == len(parsed)


# ===========================================================================
# AdventureWorks2022  (full — 71 tables)
# ===========================================================================

class TestAWFullConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn("AdventureWorks2022")
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) == 71, f"Expected 71 AW2022 tables, got {len(tables)}"

    def test_all_six_schemas_present(self):
        conn = _get_conn("AdventureWorks2022")
        schemas = {s for s, _ in _all_tables(conn)}
        conn.close()
        for expected in ("dbo", "HumanResources", "Person", "Production",
                         "Purchasing", "Sales"):
            assert expected in schemas, f"Schema '{expected}' missing"

    def test_table_counts_per_schema(self):
        conn = _get_conn("AdventureWorks2022")
        counts: dict[str, int] = {}
        for schema, _ in _all_tables(conn):
            counts[schema] = counts.get(schema, 0) + 1
        conn.close()
        assert counts.get("Production", 0) == 25, f"Expected 25 Production tables, got {counts.get('Production')}"
        assert counts.get("Sales", 0) == 19,      f"Expected 19 Sales tables, got {counts.get('Sales')}"
        assert counts.get("Person", 0) == 13,     f"Expected 13 Person tables, got {counts.get('Person')}"


class TestAWFullParse:
    def test_all_71_tables_parse_without_error(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        failures: list[tuple[str, str]] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            try:
                result = parse_ddl(ddl, dialect="tsql")
                assert result, f"parse_ddl returned empty list for {schema}.{tbl}"
            except Exception as exc:
                failures.append((f"{schema}.{tbl}", str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} AW2022 tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_employee_columns(self):
        """HumanResources.Employee has NationalIDNumber, JobTitle, DATETIME2, BIT."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "HumanResources", "Employee", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("BusinessEntityID", "NationalIDNumber", "JobTitle",
                         "BirthDate", "HireDate", "SalariedFlag"):
            assert expected in col_names, f"Column '{expected}' missing from Employee"

    def test_product_with_money_and_decimal(self):
        """Production.Product exercises MONEY, DECIMAL, SMALLINT, BIT, DATETIME."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Production", "Product", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("ProductID", "Name", "StandardCost", "ListPrice",
                         "SafetyStockLevel", "ReorderPoint", "FinishedGoodsFlag"):
            assert expected in col_names, f"Column '{expected}' missing from Production.Product"

    def test_sales_order_header_columns(self):
        """Sales.SalesOrderHeader: UNIQUEIDENTIFIER, MONEY, DATETIME, BIT, FK."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Sales", "SalesOrderHeader", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("SalesOrderID", "RevisionNumber", "OrderDate",
                         "SubTotal", "TaxAmt", "Freight", "TotalDue"):
            assert expected in col_names, f"Column '{expected}' missing from SalesOrderHeader"

    def test_person_person_udt_resolution(self):
        """Person.Person uses NameStyle (BIT UDT) and Name (NVARCHAR UDT)."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Person", "Person", udt_map)
        conn.close()

        # NameStyle UDT resolves to BIT → canonical boolean or integer
        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        name_style = next((c for c in t.columns if c.name == "NameStyle"), None)
        assert name_style is not None, "NameStyle column missing from Person.Person"
        assert name_style.type in ("boolean", "integer"), (
            f"NameStyle UDT (BIT) should map to boolean/integer, got {name_style.type}"
        )

    def test_transaction_history_high_volume_table(self):
        """Production.TransactionHistory is the largest table (113,000+ rows)."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Production", "TransactionHistory", udt_map)
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("TransactionID", "ProductID", "TransactionType",
                         "Quantity", "ActualCost"):
            assert expected in col_names


class TestAWFullEmit:
    def test_all_71_tables_emit_without_error(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        failures: list[tuple[str, str]] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            try:
                parsed = parse_ddl(ddl, dialect="tsql")
                if parsed:
                    emit_ddl(parsed[0], dialect="sqlserver")
            except Exception as exc:
                failures.append((f"{schema}.{tbl}", str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} AW2022 tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_employee(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl1 = _get_ddl(conn, "HumanResources", "Employee", udt_map)
        conn.close()

        t1   = parse_ddl(ddl1, dialect="tsql")[0]
        ddl2 = emit_ddl(t1, dialect="sqlserver", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="tsql")[0]
        ddl3 = emit_ddl(t2, dialect="sqlserver", if_not_exists=False)
        assert ddl2 == ddl3, "Employee double round-trip not idempotent"

    def test_double_roundtrip_sales_order_header(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl1 = _get_ddl(conn, "Sales", "SalesOrderHeader", udt_map)
        conn.close()

        t1   = parse_ddl(ddl1, dialect="tsql")[0]
        ddl2 = emit_ddl(t1, dialect="sqlserver", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="tsql")[0]
        ddl3 = emit_ddl(t2, dialect="sqlserver", if_not_exists=False)
        assert ddl2 == ddl3, "SalesOrderHeader double round-trip not idempotent"

    def test_cross_dialect_product_to_postgresql(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Production", "Product", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        pg_ddl = emit_ddl(t, dialect="postgresql")
        assert "CREATE TABLE" in pg_ddl
        assert "ListPrice" in pg_ddl
        assert "SafetyStockLevel" in pg_ddl

    def test_cross_dialect_employee_to_oracle(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "HumanResources", "Employee", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        ora_ddl = emit_ddl(t, dialect="oracle")
        assert "CREATE TABLE" in ora_ddl
        assert "NationalIDNumber" in ora_ddl

    def test_cross_dialect_sales_order_to_mysql(self):
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Sales", "SalesOrderDetail", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        mysql_ddl = emit_ddl(t, dialect="mysql")
        assert "CREATE TABLE" in mysql_ddl
        assert "UnitPrice" in mysql_ddl

    def test_cross_dialect_purchasing_vendor_to_databricks(self):
        """Translate to Databricks Delta Lake DDL."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Purchasing", "Vendor", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        db_ddl = emit_ddl(t, dialect="databricks")
        assert "CREATE TABLE" in db_ddl
        assert "AccountNumber" in db_ddl


class TestAWFullMultiInstance:
    def test_transaction_history_archive_expansion(self):
        """
        Production.TransactionHistoryArchive is the natural shard target:
        simulate 12 monthly shards.
        """
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Production", "TransactionHistoryArchive", udt_map)
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=12,
        )
        expanded = expand_table_instances([base])
        assert len(expanded) == 12
        assert expanded[0].name == "TransactionHistoryArchive_0001"
        assert expanded[11].name == "TransactionHistoryArchive_0012"
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="sqlserver")
            assert t.name in ddl_out

    def test_sales_order_aliases_per_region(self):
        """Sales.SalesOrderHeader with region aliases."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Sales", "SalesOrderHeader", udt_map)
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["SalesOrderHeader_EMEA", "SalesOrderHeader_APAC",
                     "SalesOrderHeader_AMER"],
        )
        expanded = expand_table_instances([base])
        assert len(expanded) == 4
        names = {t.name for t in expanded}
        assert "SalesOrderHeader" in names
        assert "SalesOrderHeader_EMEA" in names

    def test_full_aw2022_yaml_roundtrip(self):
        """All 71 tables serialise to canonical YAML and reload correctly."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        parsed: list[CanonicalTableSchema] = []
        for schema, tbl in _all_tables(conn):
            ddl = _get_ddl(conn, schema, tbl, udt_map)
            result = parse_ddl(ddl, dialect="tsql")
            if result:
                parsed.append(result[0])
        conn.close()

        assert len(parsed) == 71

        yaml_str = dump_canonical(parsed)
        assert "version:" in yaml_str
        assert "SalesOrderHeader" in yaml_str
        assert "Employee" in yaml_str
        assert len(yaml_str) > 5_000

        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == 71

    def test_aw2022_yaml_contains_expected_types(self):
        """Spot-check that canonical types appear correctly in YAML."""
        conn = _get_conn("AdventureWorks2022")
        udt_map = _build_udt_map(conn)
        ddl = _get_ddl(conn, "Production", "Product", udt_map)
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        yaml_str = dump_canonical([t])
        assert "Product" in yaml_str
        assert "ListPrice" in yaml_str
        # StandardCost is MONEY → canonical decimal
        assert "decimal" in yaml_str or "integer" in yaml_str
