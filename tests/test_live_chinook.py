"""
Live Chinook integration tests – runs against the Chinook sample database on
SQL Server 2022.

Chinook is a classic database schema modelling a digital media store (based on
the iTunes store).  It is the de-facto SQL Server sample database used in
tutorials, tools, and ORMs.  With 11 tables it is compact but covers the most
important SQL Server-specific types: NVARCHAR, DECIMAL, DATETIME, INTEGER,
NUMERIC, and composite primary keys.

Application background
----------------------
Tables represent: Artist → Album → Track (with Genre/MediaType),
Customer → Invoice → InvoiceLine, and Employee → Playlist/PlaylistTrack.
The schema comes from https://github.com/lerocha/chinook-database.

Prerequisites
-------------
Chinook database loaded into the SQL Server 22 Lima VM instance:

    # Download and load the Chinook T-SQL script
    curl -sL https://raw.githubusercontent.com/lerocha/chinook-database/master/\\
ChinookDatabase/DataSources/Chinook_SqlServer.sql -o /tmp/chinook_sqlserver.sql
    source .env
    sqlcmd -S 127.0.0.1,${SQLSERVER_PORT:-14330} -U sa -P "$SQLSERVER_PASS" \\
           -C -i /tmp/chinook_sqlserver.sql

Environment variables (defaults match the Lima VM setup):

    SQLSERVER_HOST   default: 127.0.0.1
    SQLSERVER_PORT   default: 14330
    SQLSERVER_PASS   (required; retrieve from Lima VM log — see docs)

Skip behaviour
--------------
All tests skip when SQL Server is unreachable, SQLSERVER_PASS is unset, or
pymssql is not installed — ``make test`` always completes cleanly.
"""

from __future__ import annotations

import pytest

from src.statschema import emit_ddl, load_canonical, parse_ddl
from src.statschema.model import CanonicalTableSchema, expand_table_instances
from src.statschema.schema_io import dump_schema as dump_canonical

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

from tests.live_helpers import _tp  # noqa: E402

_p = _tp("test_sqlserver")

_HOST    = _p.host     or "127.0.0.1"
_PORT    = _p.port     or 14330
_PASS    = _p.password or ""
_DB      = "Chinook"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_conn():
    if not _PASS:
        pytest.skip("SQLSERVER_PASS not set")
    mssql_python = pytest.importorskip("mssql_python")
    _user = _p.username or "sa"
    try:
        conn = mssql_python.connect(
            f"SERVER={_HOST},{_PORT};DATABASE={_DB};"
            f"UID={_user};PWD={_PASS};TrustServerCertificate=yes"
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Chinook on {_HOST}:{_PORT}: {exc}")


def _all_tables(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA='dbo' ORDER BY TABLE_NAME"
    )
    return [r[0] for r in cur.fetchall()]


def _get_ddl(conn, table: str) -> str:
    """Build a CREATE TABLE DDL string from INFORMATION_SCHEMA for SQL Server."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.COLUMN_NAME, c.DATA_TYPE,
               c.CHARACTER_MAXIMUM_LENGTH,
               c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
               c.IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_NAME = %s AND c.TABLE_SCHEMA = 'dbo'
        ORDER BY c.ORDINAL_POSITION
        """,
        (table,),
    )
    cols = cur.fetchall()

    cur.execute(
        """
        SELECT ku.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
          ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
        WHERE tc.TABLE_NAME = %s
          AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        ORDER BY ku.ORDINAL_POSITION
        """,
        (table,),
    )
    pks = [r[0] for r in cur.fetchall()]
    pk_set = set(pks)

    parts = []
    for name, dtype, maxlen, prec, scale, nullable in cols:
        col_type = dtype.upper()
        if maxlen and maxlen != -1:
            col_type += f"({maxlen})"
        elif maxlen == -1:
            col_type += "(MAX)"
        elif prec and dtype.upper() in ("NUMERIC", "DECIMAL"):
            col_type += f"({prec},{scale or 0})"
        nn = " NOT NULL" if nullable == "NO" else ""
        parts.append(f"  [{name}] {col_type}{nn}")

    if len(pks) == 1 and pks[0] in {c[0] for c in cols}:
        # Add PK inline to the single-col PK
        parts = [
            p + " PRIMARY KEY" if p.strip().startswith(f"[{pks[0]}]") else p
            for p in parts
        ]
    elif pks:
        parts.append(f"  CONSTRAINT PK_{table} PRIMARY KEY ({', '.join('[' + p + ']' for p in pks)})")

    return f"CREATE TABLE [dbo].[{table}] (\n" + ",\n".join(parts) + "\n)"


# ---------------------------------------------------------------------------
# Test: database connectivity
# ---------------------------------------------------------------------------

class TestChinookConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) == 11, f"Expected 11 Chinook tables, got {len(tables)}"

    def test_all_chinook_tables_present(self):
        conn = _get_conn()
        tables = set(_all_tables(conn))
        conn.close()
        expected = {
            "Album", "Artist", "Customer", "Employee", "Genre",
            "Invoice", "InvoiceLine", "MediaType", "Playlist",
            "PlaylistTrack", "Track",
        }
        missing = expected - tables
        assert not missing, f"Missing Chinook tables: {missing}"


# ---------------------------------------------------------------------------
# Test: parse all Chinook tables
# ---------------------------------------------------------------------------

class TestChinookParse:
    def test_all_tables_parse_without_error(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                result = parse_ddl(ddl, dialect="tsql")
                assert result, f"parse_ddl returned empty list for {tbl}"
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_track_table_columns(self):
        """Track is the richest table — NVARCHAR, INTEGER, DECIMAL, FLOAT."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "Track")
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("TrackId", "Name", "AlbumId", "Milliseconds", "UnitPrice"):
            assert expected in col_names, f"Column '{expected}' missing from Track"

    def test_invoice_table_columns(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Invoice")
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("InvoiceId", "CustomerId", "InvoiceDate", "Total"):
            assert expected in col_names, f"Column '{expected}' missing from Invoice"

    def test_playlist_track_composite_pk(self):
        """PlaylistTrack has a composite primary key — verify both PK cols parsed."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "PlaylistTrack")
        conn.close()

        tables = parse_ddl(ddl, dialect="tsql")
        assert tables
        t = tables[0]
        pk_cols = [c for c in t.columns if c.primary_key]
        # Both PlaylistId and TrackId should be marked as PK
        pk_names = {c.name for c in pk_cols}
        assert "PlaylistId" in pk_names or "TrackId" in pk_names, (
            f"Expected composite PK columns in PlaylistTrack, got: {pk_names}"
        )


# ---------------------------------------------------------------------------
# Test: emit DDL for all Chinook tables
# ---------------------------------------------------------------------------

class TestChinookEmit:
    def test_all_tables_emit_without_error(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                parsed = parse_ddl(ddl, dialect="tsql")
                if parsed:
                    emit_ddl(parsed[0], dialect="sqlserver")
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_track(self):
        conn = _get_conn()
        ddl1 = _get_ddl(conn, "Track")
        conn.close()

        t1   = parse_ddl(ddl1, dialect="tsql")[0]
        # Use if_not_exists=False so the plain CREATE TABLE can be re-parsed
        ddl2 = emit_ddl(t1, dialect="sqlserver", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="tsql")[0]
        ddl3 = emit_ddl(t2, dialect="sqlserver", if_not_exists=False)

        assert ddl2 == ddl3, "Track table double round-trip not idempotent"

    def test_cross_dialect_invoice_to_mysql(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Invoice")
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        mysql_ddl = emit_ddl(t, dialect="mysql")
        assert "CREATE TABLE" in mysql_ddl
        assert "InvoiceDate" in mysql_ddl
        assert "Total" in mysql_ddl

    def test_cross_dialect_customer_to_postgresql(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Customer")
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        pg_ddl = emit_ddl(t, dialect="postgresql")
        assert "CREATE TABLE" in pg_ddl
        assert "Email" in pg_ddl

    def test_cross_dialect_track_to_oracle(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Track")
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        ora_ddl = emit_ddl(t, dialect="oracle")
        assert "CREATE TABLE" in ora_ddl
        assert "Milliseconds" in ora_ddl


# ---------------------------------------------------------------------------
# Test: multi-instance YAML and canonical features
# ---------------------------------------------------------------------------

class TestChinookMultiInstance:
    def test_track_shard_expansion(self):
        """Simulate sharding Track into 5 numbered instances."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "Track")
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=5,
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 5
        names = [t.name for t in expanded]
        assert names == [
            "Track_0001", "Track_0002", "Track_0003", "Track_0004", "Track_0005"
        ]
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="sqlserver")
            assert t.name in ddl_out

    def test_invoice_with_aliases(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Invoice")
        conn.close()

        base = parse_ddl(ddl, dialect="tsql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["Invoice_Archive", "Invoice_2024", "Invoice_2025"],
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 4  # base + 3 aliases
        names = {t.name for t in expanded}
        assert "Invoice" in names
        assert "Invoice_Archive" in names
        assert "Invoice_2024" in names

    def test_yaml_roundtrip_artist_album(self):
        """Verify both Artist and Album serialise and round-trip through YAML."""
        conn = _get_conn()
        artist_ddl = _get_ddl(conn, "Artist")
        album_ddl  = _get_ddl(conn, "Album")
        conn.close()

        tables = [
            parse_ddl(artist_ddl, dialect="tsql")[0],
            parse_ddl(album_ddl, dialect="tsql")[0],
        ]
        yaml_str = dump_canonical(tables)
        reloaded = load_canonical(yaml_str)

        assert len(reloaded) == 2
        assert {t.name for t in reloaded} == {"Artist", "Album"}


# ---------------------------------------------------------------------------
# Test: Chinook full schema dump
# ---------------------------------------------------------------------------

class TestChinookCanonicalYaml:
    def test_full_schema_dumps_to_yaml(self):
        conn = _get_conn()
        tables_raw = _all_tables(conn)
        parsed: list[CanonicalTableSchema] = []
        for tbl in tables_raw:
            ddl = _get_ddl(conn, tbl)
            result = parse_ddl(ddl, dialect="tsql")
            if result:
                parsed.append(result[0])
        conn.close()

        yaml_str = dump_canonical(parsed)
        assert "version:" in yaml_str
        assert "tables:" in yaml_str
        assert len(yaml_str) > 500

        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == len(parsed)

    def test_track_yaml_contains_key_fields(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "Track")
        conn.close()

        t = parse_ddl(ddl, dialect="tsql")[0]
        yaml_str = dump_canonical([t])
        assert "Track" in yaml_str
        assert "UnitPrice" in yaml_str
        assert "Milliseconds" in yaml_str
