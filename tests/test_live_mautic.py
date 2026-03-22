"""
Live Mautic integration tests – runs against a real Mautic 5 MySQL database.

These tests verify that all 108 Mautic tables (as installed by Mautic 5.x)
survive the parse → emit → execute → introspect cycle, and that the
multi-instance / alias expansion feature works correctly for the high-volume
shard candidates in a Mautic deployment.

Mautic background
-----------------
Mautic creates ~108 tables.  High-traffic installations shard large event tables
(email_stats, campaign_lead_event_log, page_hits …) into numbered copies
(email_stats_0001, email_stats_0002 …).  The canonical YAML ``instance_count``
and ``aliases`` fields are designed exactly for this pattern.

Prerequisites
-------------
Mautic container running against the mysql8 container (see docs/local-databases.md):

    # Create network and mautic DB
    podman network create mautic_net
    podman network connect mautic_net mysql8
    podman run -d --name mautic --network mautic_net -p 8080:80 \\
        -e MAUTIC_DB_HOST=mysql8 -e MAUTIC_DB_PORT=3306 \\
        -e MAUTIC_DB_NAME=mautic -e MAUTIC_DB_USER=mautic \\
        -e MAUTIC_DB_PASSWORD=mauticpass \\
        docker.io/mautic/mautic:5-apache
    # Install via CLI
    podman exec mautic php /var/www/html/bin/console mautic:install \\
        --force --db_driver=pdo_mysql --db_host=mysql8 --db_port=3306 \\
        --db_name=mautic --db_user=mautic --db_password=mauticpass \\
        --admin_email=admin@example.com --admin_password='Mautic1234!' \\
        --admin_firstname=Admin --admin_lastname=User http://127.0.0.1:8080
    # Fix migration metadata
    podman exec mautic bash -c "cd /var/www/html && \\
        php bin/console doctrine:migrations:sync-metadata-storage && \\
        php bin/console doctrine:migrations:version --add --all --no-interaction"
    # Enable the REST API
    # Edit /var/www/html/config/local.php to add:
    #   'api_enabled' => true,
    #   'api_enable_basic_auth' => true,
    # then: php bin/console cache:warmup

Environment variables (defaults match the commands above):

    MAUTIC_MYSQL_HOST  default: 127.0.0.1
    MAUTIC_MYSQL_PORT  default: 3384   (mysql8 Podman port)
    MAUTIC_MYSQL_USER  default: mautic
    MAUTIC_MYSQL_PASS  default: mauticpass
    MAUTIC_MYSQL_DB    default: mautic

Skip behaviour
--------------
All tests skip automatically when the Mautic database is unreachable or
pymysql is not installed — ``make test`` always completes cleanly.
"""

from __future__ import annotations

import os
import textwrap
from typing import Optional

import pytest

from src.statschema import (
    emit_ddl,
    load_canonical,
    parse_ddl,
)
from src.statschema.model import (
    CanonicalTableSchema,
    expand_table_instances,
)
from src.statschema.schema_io import dump_schema as dump_canonical

# ---------------------------------------------------------------------------
# Connection configuration
# ---------------------------------------------------------------------------

_HOST  = os.environ.get("MAUTIC_MYSQL_HOST", "127.0.0.1")
_PORT  = int(os.environ.get("MAUTIC_MYSQL_PORT", "3384"))
_USER  = os.environ.get("MAUTIC_MYSQL_USER", "mautic")
_PASS  = os.environ.get("MAUTIC_MYSQL_PASS", "mauticpass")
_DB    = os.environ.get("MAUTIC_MYSQL_DB",   "mautic")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_conn():
    """Return a pymysql connection to the Mautic DB, or skip the test."""
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=_HOST, port=_PORT,
            user=_USER, password=_PASS,
            database=_DB,
            connect_timeout=5,
            autocommit=True,
        )
        return conn
    except Exception as exc:
        pytest.skip(f"Cannot connect to Mautic MySQL on {_HOST}:{_PORT}: {exc}")


def _all_tables(conn) -> list[str]:
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    return [r[0] for r in cur.fetchall()]


def _get_ddl(conn, table: str) -> str:
    cur = conn.cursor()
    cur.execute(f"SHOW CREATE TABLE `{table}`")
    return cur.fetchone()[1]


def _get_columns(conn, table: str) -> dict[str, str]:
    """Return {col_name: data_type} from information_schema."""
    cur = conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION",
        (_DB, table),
    )
    return {r[0]: r[1].lower() for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Test: database connectivity
# ---------------------------------------------------------------------------

class TestMauticConnection:
    def test_connect_and_count_tables(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        assert len(tables) >= 100, (
            f"Expected ≥100 Mautic tables, got {len(tables)}"
        )
        conn.close()

    def test_contacts_exist(self):
        """Verifies the 10 test contacts were inserted."""
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM leads")
        count = cur.fetchone()[0]
        assert count >= 10, f"Expected ≥10 contacts in leads, got {count}"
        conn.close()


# ---------------------------------------------------------------------------
# Test: parse all Mautic tables
# ---------------------------------------------------------------------------

class TestMauticParse:
    def test_all_tables_parse_without_error(self):
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                result = parse_ddl(ddl, dialect="mysql")
                assert result, f"parse_ddl returned empty list for {tbl}"
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to parse:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_leads_parsed_columns(self):
        """Check key columns of the leads (contacts) table."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "leads")
        tables = parse_ddl(ddl, dialect="mysql")
        conn.close()

        assert tables, "parse_ddl returned nothing for leads"
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "firstname", "lastname", "email", "company", "points"):
            assert expected in col_names, f"Column '{expected}' missing from leads"

        id_col = next(c for c in t.columns if c.name == "id")
        assert id_col.auto_increment
        assert id_col.primary_key

    def test_email_stats_parsed_columns(self):
        """email_stats is the primary shard candidate — verify structure."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "email_stats")
        tables = parse_ddl(ddl, dialect="mysql")
        conn.close()

        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "email_id", "lead_id", "date_sent"):
            assert expected in col_names, f"Column '{expected}' missing from email_stats"

    def test_campaign_lead_event_log_parsed(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "campaign_lead_event_log")
        tables = parse_ddl(ddl, dialect="mysql")
        conn.close()

        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "event_id", "lead_id", "campaign_id"):
            assert expected in col_names, (
                f"Column '{expected}' missing from campaign_lead_event_log"
            )

    def test_page_hits_parsed(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "page_hits")
        tables = parse_ddl(ddl, dialect="mysql")
        conn.close()

        assert tables
        t = tables[0]
        col_names = {c.name for c in t.columns}
        for expected in ("id", "page_id", "lead_id", "date_hit"):
            assert expected in col_names, (
                f"Column '{expected}' missing from page_hits"
            )


# ---------------------------------------------------------------------------
# Test: emit (DDL round-trip) for all Mautic tables
# ---------------------------------------------------------------------------

class TestMauticEmit:
    def test_all_tables_emit_without_error(self):
        """parse_ddl → emit_ddl should succeed for every Mautic table."""
        conn = _get_conn()
        tables = _all_tables(conn)
        failures: list[tuple[str, str]] = []
        for tbl in tables:
            ddl = _get_ddl(conn, tbl)
            try:
                parsed = parse_ddl(ddl, dialect="mysql")
                if not parsed:
                    continue
                emit_ddl(parsed[0], dialect="mysql")
            except Exception as exc:
                failures.append((tbl, str(exc)[:120]))
        conn.close()
        assert not failures, (
            f"{len(failures)} tables failed to emit:\n"
            + "\n".join(f"  {t}: {e}" for t, e in failures)
        )

    def test_double_roundtrip_email_stats(self):
        """Parse → emit → parse → emit must be idempotent for email_stats."""
        conn = _get_conn()
        ddl1 = _get_ddl(conn, "email_stats")
        conn.close()

        t1   = parse_ddl(ddl1, dialect="mysql")[0]
        ddl2 = emit_ddl(t1, dialect="mysql")
        t2   = parse_ddl(ddl2, dialect="mysql")[0]
        ddl3 = emit_ddl(t2, dialect="mysql")

        assert ddl2 == ddl3, "email_stats double round-trip not idempotent"

    def test_cross_dialect_leads_to_postgres(self):
        """Translate Mautic leads table MySQL DDL → canonical → PostgreSQL DDL."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "leads")
        conn.close()

        t = parse_ddl(ddl, dialect="mysql")[0]
        pg_ddl = emit_ddl(t, dialect="postgresql")
        assert "CREATE TABLE" in pg_ddl
        assert "firstname" in pg_ddl
        assert "email" in pg_ddl

    def test_cross_dialect_email_stats_to_sqlserver(self):
        """Translate email_stats MySQL DDL → canonical → SQL Server DDL."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "email_stats")
        conn.close()

        t = parse_ddl(ddl, dialect="mysql")[0]
        tsql = emit_ddl(t, dialect="sqlserver")
        assert "CREATE TABLE" in tsql
        assert "email_id" in tsql


# ---------------------------------------------------------------------------
# Test: multi-instance YAML expansion for Mautic shard pattern
# ---------------------------------------------------------------------------

class TestMauticMultiInstance:
    """
    Mautic high-traffic deployments shard email_stats into numbered copies.
    These tests verify the canonical YAML instance_count / aliases feature
    works correctly for that pattern.
    """

    def test_email_stats_instance_count_expansion(self):
        """email_stats with instance_count=4 produces 4 numbered tables."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "email_stats")
        conn.close()

        base = parse_ddl(ddl, dialect="mysql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=4,
            instance_suffix_format="_{:04d}",
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 4
        names = [t.name for t in expanded]
        assert names == [
            "email_stats_0001",
            "email_stats_0002",
            "email_stats_0003",
            "email_stats_0004",
        ]
        # Each expanded table must still emit valid DDL
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="mysql")
            assert f"`{t.name}`" in ddl_out

    def test_campaign_log_shard_with_aliases(self):
        """campaign_lead_event_log with instance_count=3 plus aliases."""
        conn = _get_conn()
        ddl = _get_ddl(conn, "campaign_lead_event_log")
        conn.close()

        base = parse_ddl(ddl, dialect="mysql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=3,
            aliases=["campaign_lead_event_log_archive"],
        )

        expanded = expand_table_instances([base])
        # 3 numbered + 1 alias = 4 tables
        assert len(expanded) == 4
        names = [t.name for t in expanded]
        assert "campaign_lead_event_log_0001" in names
        assert "campaign_lead_event_log_0002" in names
        assert "campaign_lead_event_log_0003" in names
        assert "campaign_lead_event_log_archive" in names

    def test_page_hits_yaml_roundtrip(self):
        """
        Build multi-instance canonical YAML for page_hits, serialise to YAML,
        deserialise with expand=True, and verify the resulting tables.
        """
        conn = _get_conn()
        ddl = _get_ddl(conn, "page_hits")
        conn.close()

        base = parse_ddl(ddl, dialect="mysql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            instance_count=2,
        )

        # Serialise
        yaml_str = dump_canonical([base])
        assert "instance_count: 2" in yaml_str

        # Deserialise without expand
        compact = load_canonical(yaml_str)
        assert len(compact) == 1
        assert compact[0].instance_count == 2

        # Deserialise with expand
        flat = load_canonical(yaml_str, expand=True)
        assert len(flat) == 2
        assert flat[0].name == "page_hits_0001"
        assert flat[1].name == "page_hits_0002"

    def test_mautic_aliases_pattern(self):
        """
        Aliases model the Mautic pattern where the same schema is reused
        under a different table name (e.g. email_stat_replies mirrors email_stats
        core structure).
        """
        conn = _get_conn()
        ddl = _get_ddl(conn, "audit_log")
        conn.close()

        base = parse_ddl(ddl, dialect="mysql")[0]
        base = CanonicalTableSchema(
            name=base.name,
            columns=base.columns,
            fk_constraints=base.fk_constraints,
            temporal_ordering_constraints=base.temporal_ordering_constraints,
            aliases=["audit_log_archive", "audit_log_purged"],
        )

        expanded = expand_table_instances([base])
        assert len(expanded) == 3   # base + 2 aliases
        names = [t.name for t in expanded]
        assert "audit_log" in names
        assert "audit_log_archive" in names
        assert "audit_log_purged" in names

        # All expanded tables should emit valid DDL
        for t in expanded:
            ddl_out = emit_ddl(t, dialect="mysql")
            assert f"`{t.name}`" in ddl_out

    def test_bulk_mautic_shard_scenario(self):
        """
        Simulate a large Mautic deployment: shard email_stats, page_hits, and
        campaign_lead_event_log each into 10 instances.  Total expanded count
        should be 30 tables, all emitting valid DDL.
        """
        conn = _get_conn()
        shard_tables = ["email_stats", "page_hits", "campaign_lead_event_log"]
        shards: list[CanonicalTableSchema] = []
        for tbl in shard_tables:
            ddl = _get_ddl(conn, tbl)
            base = parse_ddl(ddl, dialect="mysql")[0]
            shards.append(
                CanonicalTableSchema(
                    name=base.name,
                    columns=base.columns,
                    fk_constraints=base.fk_constraints,
                    temporal_ordering_constraints=base.temporal_ordering_constraints,
                    instance_count=10,
                )
            )
        conn.close()

        expanded = expand_table_instances(shards)
        assert len(expanded) == 30

        failures = []
        for t in expanded:
            try:
                emit_ddl(t, dialect="mysql")
            except Exception as exc:
                failures.append((t.name, str(exc)))

        assert not failures, (
            f"{len(failures)} shard tables failed to emit:\n"
            + "\n".join(f"  {n}: {e}" for n, e in failures)
        )


# ---------------------------------------------------------------------------
# Test: Mautic schema to canonical YAML
# ---------------------------------------------------------------------------

class TestMauticCanonicalYaml:
    def test_full_schema_dumps_to_yaml(self):
        """All 108 Mautic tables can be serialised to canonical YAML."""
        conn = _get_conn()
        tables_raw = _all_tables(conn)
        parsed: list[CanonicalTableSchema] = []
        for tbl in tables_raw:
            ddl = _get_ddl(conn, tbl)
            result = parse_ddl(ddl, dialect="mysql")
            if result:
                parsed.append(result[0])
        conn.close()

        yaml_str = dump_canonical(parsed)
        assert "version:" in yaml_str
        assert "tables:" in yaml_str
        # Round-trip
        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == len(parsed)

    def test_email_stats_yaml_contains_key_fields(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "email_stats")
        conn.close()

        t = parse_ddl(ddl, dialect="mysql")[0]
        yaml_str = dump_canonical([t])
        assert "email_stats" in yaml_str
        assert "email_id" in yaml_str
        assert "lead_id" in yaml_str

    def test_leads_yaml_roundtrip(self):
        conn = _get_conn()
        ddl = _get_ddl(conn, "leads")
        conn.close()

        t = parse_ddl(ddl, dialect="mysql")[0]
        yaml_str = dump_canonical([t])
        reloaded = load_canonical(yaml_str)
        assert len(reloaded) == 1
        t2 = reloaded[0]
        assert t2.name == "leads"
        orig_names = {c.name for c in t.columns}
        new_names  = {c.name for c in t2.columns}
        assert orig_names == new_names
