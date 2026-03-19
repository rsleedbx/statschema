"""
Live Gitea integration tests – runs against a Gitea 1.x PostgreSQL database.

Gitea is a self-hosted Git service written in Go with ~112 PostgreSQL tables.
It is an excellent PostgreSQL test target because it exercises diverse PG types
(BIGINT, BOOLEAN, TEXT, TIMESTAMP WITH TIME ZONE, BLOB/BYTEA, JSONB, etc.)
and real-world FK relationships.

Application background
----------------------
Gitea stores repositories, users, issues, pull requests, CI/CD pipelines (actions),
packages, and more.  Tables with natural shard patterns include action_log,
notification, and issue_comment.

Prerequisites
-------------
Gitea running against the pg16 container (see docs/local-databases.md):

    # Create gitea user and database on pg16
    podman exec pg16 psql -U postgres -c "
      CREATE USER gitea WITH PASSWORD 'gitea123';
      CREATE DATABASE gitea OWNER gitea;
    "
    # Create a dedicated network and connect pg16
    podman network create gitea_net
    podman network connect gitea_net pg16
    # Run Gitea (auto-installs schema on first start)
    podman run -d --name gitea --network gitea_net -p 3000:3000 \\
        -e GITEA__database__DB_TYPE=postgres \\
        -e GITEA__database__HOST=pg16:5432 \\
        -e GITEA__database__NAME=gitea \\
        -e GITEA__database__USER=gitea \\
        -e GITEA__database__PASSWD=gitea123 \\
        -e GITEA__security__INSTALL_LOCK=true \\
        -e GITEA__security__SECRET_KEY=zerobus_test_secret_key_32chars0 \\
        -e GITEA__server__ROOT_URL=http://localhost:3000/ \\
        docker.io/gitea/gitea:latest
    # Wait ~30s for schema creation, then create an admin user
    podman exec -u git gitea gitea admin user create \\
        --admin --username=gitadmin --password=Gitpass123! \\
        --email=admin@example.com --must-change-password=false

Environment variables (defaults match the commands above):

    GITEA_PG_HOST  default: 127.0.0.1
    GITEA_PG_PORT  default: 5416   (pg16 Podman port)
    GITEA_PG_USER  default: gitea
    GITEA_PG_PASS  default: gitea123
    GITEA_PG_DB    default: gitea

Skip behaviour
--------------
All tests skip when the Gitea database is unreachable or psycopg2 is not
installed — ``make test`` always completes cleanly.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from src.schema_parser import emit_ddl, load_canonical, parse_ddl
from src.schema_parser.model import CanonicalTableSchema, expand_table_instances
from src.schema_parser.schema_io import dump_schema as dump_canonical

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_HOST = os.environ.get("GITEA_PG_HOST", "127.0.0.1")
_PORT = int(os.environ.get("GITEA_PG_PORT", "5416"))
_USER = os.environ.get("GITEA_PG_USER", "gitea")
_PASS = os.environ.get("GITEA_PG_PASS", "gitea123")
_DB   = os.environ.get("GITEA_PG_DB",   "gitea")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_conn():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(
            host=_HOST, port=_PORT, user=_USER, password=_PASS, dbname=_DB,
            connect_timeout=5, options="-c search_path=public"
        )
        conn.autocommit = True
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Gitea PostgreSQL on {_HOST}:{_PORT}: {exc}")


def _all_tables(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname='public' ORDER BY tablename"
    )
    return [r[0] for r in cur.fetchall()]


def _get_ddl(conn, table: str) -> str:
    """Build a CREATE TABLE DDL string from information_schema."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name,
               CASE
                 WHEN data_type = 'character varying'
                   THEN 'VARCHAR(' || COALESCE(character_maximum_length::text, '255') || ')'
                 WHEN data_type = 'character'
                   THEN 'CHAR(' || COALESCE(character_maximum_length::text, '1') || ')'
                 WHEN data_type = 'numeric'
                   THEN 'NUMERIC(' || COALESCE(numeric_precision::text, '18') ||
                        CASE WHEN numeric_scale IS NOT NULL THEN ',' || numeric_scale ELSE '' END || ')'
                 ELSE data_type
               END,
               is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    cols = cur.fetchall()
    if not cols:
        return f"CREATE TABLE {table} (id BIGINT)"

    # Get primary key columns
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (table,),
    )
    pks = {r[0] for r in cur.fetchall()}

    parts = []
    for col_name, col_type, nullable in cols:
        nn = " NOT NULL" if nullable == "NO" else ""
        pk = " PRIMARY KEY" if col_name in pks and len(pks) == 1 else ""
        parts.append(f"  {col_name} {col_type}{nn}{pk}")
    if len(pks) > 1:
        parts.append(f"  PRIMARY KEY ({', '.join(pks)})")

    return f"CREATE TABLE {table} (\n" + ",\n".join(parts) + "\n)"


# ---------------------------------------------------------------------------
# Test: database connectivity
# ---------------------------------------------------------------------------

class TestGiteaConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        conn.close()
        assert len(tables) >= 100, (
            f"Expected ≥100 Gitea tables, got {len(tables)}"
        )

    def test_key_tables_exist(self):
        """Verify core Gitea tables are present."""
        conn = _get_conn()
        tables = set(_all_tables(conn))
        conn.close()
        for expected in ("user", "repository", "issue", "pull_request",
                         "action_run", "access_token", "notification"):
            assert expected in tables, f"Gitea table '{expected}' missing"


# ---------------------------------------------------------------------------
# Test: parse all Gitea tables
# ---------------------------------------------------------------------------

class TestGiteaParse:
    def test_all_tables_parse_without_error(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                result = parse_ddl(ddl, dialect="postgresql")
                assert result, f"parse_ddl returned empty list for {tbl}"
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_user_table_columns(self):
        """Check key columns of the user (accounts) table."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "user")
        conn.close()

        tables = parse_ddl(ddl, dialect="postgresql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "name", "email", "passwd"):
            assert expected in col_names, f"Column '{expected}' missing from user"

        id_col = next(c for c in t.columns if c.name == "id")
        assert id_col.primary_key

    def test_repository_table_columns(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "repository")
        conn.close()

        tables = parse_ddl(ddl, dialect="postgresql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "owner_id", "name", "is_private"):
            assert expected in col_names, f"Column '{expected}' missing from repository"

    def test_issue_table_columns(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "issue")
        conn.close()

        tables = parse_ddl(ddl, dialect="postgresql")
        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "repo_id", "poster_id", "content"):
            assert expected in col_names, f"Column '{expected}' missing from issue"


# ---------------------------------------------------------------------------
# Test: emit DDL for all Gitea tables
# ---------------------------------------------------------------------------

class TestGiteaEmit:
    def test_all_tables_emit_without_error(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                parsed = parse_ddl(ddl, dialect="postgresql")
                if parsed:
                    emit_ddl(parsed[0], dialect="postgresql")
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_user(self):
        """Parse → emit → parse → emit must be idempotent for the user table."""
        conn = _get_conn()
        ddl1 = _get_ddl(conn, "user")
        conn.close()

        t1   = parse_ddl(ddl1, dialect="postgresql")[0]
        ddl2 = emit_ddl(t1, dialect="postgresql", if_not_exists=False)
        t2   = parse_ddl(ddl2, dialect="postgresql")[0]
        ddl3 = emit_ddl(t2, dialect="postgresql", if_not_exists=False)

        assert ddl2 == ddl3, "user table double round-trip not idempotent"

    def test_cross_dialect_repository_to_mysql(self):
        """Translate Gitea repository DDL: PostgreSQL → canonical → MySQL."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "repository")
        conn.close()

        t = parse_ddl(ddl, dialect="postgresql")[0]
        mysql_ddl = emit_ddl(t, dialect="mysql")
        assert "CREATE TABLE" in mysql_ddl
        assert "owner_id" in mysql_ddl
        assert "is_private" in mysql_ddl

    def test_cross_dialect_issue_to_sqlserver(self):
        """Translate Gitea issue table: PostgreSQL → canonical → SQL Server DDL."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "issue")
        conn.close()

        t = parse_ddl(ddl, dialect="postgresql")[0]
        tsql = emit_ddl(t, dialect="sqlserver")
        assert "CREATE TABLE" in tsql
        assert "repo_id" in tsql

    def test_cross_dialect_to_oracle(self):
        """Translate action_run table: PostgreSQL → canonical → Oracle DDL."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "action_run")
        conn.close()

        t = parse_ddl(ddl, dialect="postgresql")[0]
        oracle_ddl = emit_ddl(t, dialect="oracle")
        assert "CREATE TABLE" in oracle_ddl


# ---------------------------------------------------------------------------
# Test: multi-instance YAML expansion for Gitea shard patterns
# ---------------------------------------------------------------------------

class TestGiteaMultiInstance:
    def test_action_task_shard_expansion(self):
        """
        CI/CD action_task grows large in busy Gitea instances.
        Verify 3-shard expansion works correctly.
        """
        conn = _get_conn()
        ddl = _get_ddl(conn, "action_task")
        conn.close()

        base = parse_ddl(ddl, dialect="postgresql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=3,
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 3
        names = [t.name for t in expanded]
        assert names == ["action_task_0001", "action_task_0002", "action_task_0003"]
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="postgresql")
            assert f'"{t.name}"' in ddl_out or t.name in ddl_out

    def test_notification_aliases(self):
        """notification table with aliases for archive and unread copies."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "notification")
        conn.close()

        base = parse_ddl(ddl, dialect="postgresql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["notification_archive", "notification_unread"],
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 3
        names = [t.name for t in expanded]
        assert "notification" in names
        assert "notification_archive" in names
        assert "notification_unread" in names

    def test_yaml_roundtrip_issue_comment(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "comment")
        conn.close()

        base = parse_ddl(ddl, dialect="postgresql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=2,
        )

        yaml_str = dump_canonical([base])
        assert "instance_count: 2" in yaml_str

        flat = load_canonical(yaml_str, expand=True)
        assert len(flat) == 2
        assert flat[0].name == "comment_0001"
        assert flat[1].name == "comment_0002"


# ---------------------------------------------------------------------------
# Test: Gitea schema to canonical YAML
# ---------------------------------------------------------------------------

class TestGiteaCanonicalYaml:
    def test_full_schema_dumps_and_reloads(self):
        """All Gitea tables can be serialised to canonical YAML and reloaded."""
        conn = _get_conn()
        tables_raw = _all_tables(conn)
        parsed: list[CanonicalTableSchema] = []
        for tbl in tables_raw:
            ddl = _get_ddl(conn, tbl)
            result = parse_ddl(ddl, dialect="postgresql")
            if result:
                parsed.append(result[0])
        conn.close()

        yaml_str = dump_canonical(parsed)
        assert "version:" in yaml_str
        assert "tables:" in yaml_str

        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == len(parsed)

    def test_repository_yaml_contains_key_fields(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "repository")
        conn.close()

        t = parse_ddl(ddl, dialect="postgresql")[0]
        yaml_str = dump_canonical([t])
        assert "repository" in yaml_str
        assert "owner_id" in yaml_str
        assert "is_private" in yaml_str
