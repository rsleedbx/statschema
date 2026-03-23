"""
Offline CLI smoke-tests for all five statschema subcommands.

  ddl      — emit DDL to stdout from a YAML schema
  generate — stream synthetic rows to CSV / JSONL files
  load     — insert synthetic rows into a SQLite database
  collect  — extract schema from a SQLite database to YAML
  inject   — error path: unsupported dialect (no live connection needed)

  README   — verify that every code and YAML snippet in the Quick-start
             section of README.md executes correctly.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from src.statschema.cli import main

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SIMPLE_SCHEMA_YAML = """\
tables:
  - name: customers
    primary_key: [id]
    row_count_per_sf: 100
    columns:
      - name: id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: name
        type: varchar
        length: 50
        not_null: true
        generation: {values: [Alice, Bob, Carol, Dave], weights: [0.4, 0.3, 0.2, 0.1]}
      - name: score
        type: decimal
        precision: 5
        scale: 2
        generation: {distribution: normal, mean: 50.0, std: 15.0, min_value: 0.0, max_value: 100.0}

  - name: orders
    primary_key: [id]
    row_count_per_sf: 300
    columns:
      - name: id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: customer_id
        type: integer
        not_null: true
      - name: total
        type: decimal
        precision: 10
        scale: 2
        not_null: true
        generation: {distribution: normal, mean: 50.0, std: 20.0, min_value: 1.0, max_value: 500.0}
    fk_constraints:
      - columns: [customer_id]
        parent_table: customers
        parent_columns: [id]
"""

# README Quick-start YAML (orders / order_details with Zipf + weights + conditional nulls)
_README_ORDERS_YAML = """\
tables:
  - name: orders
    primary_key: [order_id]
    row_count_per_sf: 1000
    columns:
      - name: order_id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: customer_id
        type: integer
        not_null: true
        generation: {distribution: zipf, min_value: 1, max_value: 50000}
      - name: status
        type: varchar
        length: 20
        not_null: true
        generation:
          values:   [pending, processing, shipped, delivered, cancelled]
          weights:  [0.08,    0.12,       0.25,    0.50,      0.05]
      - name: order_date
        type: date
        not_null: true
        generation: {distribution: uniform, min_value: "2023-01-01", max_value: "2024-12-31"}
      - name: ship_date
        type: date
        generation:
          distribution: uniform
          min_value: "2023-01-15"
          max_value: "2025-01-31"
          null_rate: 0.08

  - name: order_details
    primary_key: [detail_id]
    row_count_per_sf: 3500
    columns:
      - name: detail_id
        type: integer
        not_null: true
        generation: {distribution: sequential, min_value: 1}
      - name: order_id
        type: integer
        not_null: true
      - name: product_id
        type: integer
        not_null: true
        generation: {distribution: zipf, min_value: 1, max_value: 10000}
      - name: quantity
        type: integer
        not_null: true
        generation: {distribution: normal, mean: 3, std: 2, min_value: 1, max_value: 100}
      - name: unit_price
        type: decimal
        precision: 10
        scale: 2
        not_null: true
        generation: {distribution: normal, mean: 49.99, std: 30.0, min_value: 0.99, max_value: 999.99}
      - name: discount_pct
        type: decimal
        precision: 5
        scale: 2
        generation:
          distribution: zipf
          min_value: 0
          max_value: 50
          null_rate: 0.65
    fk_constraints:
      - columns: [order_id]
        parent_table: orders
        parent_columns: [order_id]
"""


@pytest.fixture
def schema_yaml(tmp_path: Path) -> str:
    p = tmp_path / "schema.yaml"
    p.write_text(_SIMPLE_SCHEMA_YAML)
    return str(p)


# ---------------------------------------------------------------------------
# ddl subcommand
# ---------------------------------------------------------------------------

class TestDDLSubcommand:

    @pytest.mark.parametrize("dialect", ["postgres", "oracle", "sqlserver", "databricks", "mysql", "db2"])
    def test_emits_create_table_for_all_dialects(self, schema_yaml, dialect):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["ddl", schema_yaml, "--dialect", dialect])
        out = buf.getvalue()
        assert "CREATE TABLE" in out.upper()
        # Both tables must appear
        assert "customers" in out.lower()
        assert "orders" in out.lower()

    def test_fk_table_ordering(self, schema_yaml):
        """Topological sort: customers must appear before orders in the DDL output."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["ddl", schema_yaml, "--dialect", "postgres"])
        out = buf.getvalue()
        assert out.index("customers") < out.index("orders")


# ---------------------------------------------------------------------------
# generate subcommand
# ---------------------------------------------------------------------------

class TestGenerateSubcommand:

    def test_generate_csv_files(self, schema_yaml, tmp_path):
        out_dir = tmp_path / "out"
        main(["generate", schema_yaml, "--sf", "0.1", "--format", "csv", "--out-dir", str(out_dir)])

        customers_csv = out_dir / "customers.csv"
        orders_csv    = out_dir / "orders.csv"
        assert customers_csv.exists(), "customers.csv not created"
        assert orders_csv.exists(),    "orders.csv not created"

        # row_count_per_sf * sf: 100 * 0.1 = 10 customers, 300 * 0.1 = 30 orders
        with customers_csv.open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 10

        with orders_csv.open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 30

    def test_generate_jsonl_files(self, schema_yaml, tmp_path):
        out_dir = tmp_path / "out"
        main(["generate", schema_yaml, "--sf", "0.1", "--format", "jsonl", "--out-dir", str(out_dir)])

        jsonl_file = out_dir / "customers.jsonl"
        assert jsonl_file.exists()
        lines = jsonl_file.read_text().strip().splitlines()
        assert len(lines) == 10
        # Every line must parse as valid JSON with the expected columns
        for line in lines:
            row = json.loads(line)
            assert {"id", "name", "score"} <= row.keys()

    def test_generate_csv_fk_integrity(self, schema_yaml, tmp_path):
        """Every orders.customer_id must reference an existing customers.id."""
        out_dir = tmp_path / "fk"
        main(["generate", schema_yaml, "--sf", "1.0", "--format", "csv", "--out-dir", str(out_dir), "--seed", "42"])

        with (out_dir / "customers.csv").open() as fh:
            customer_ids = {int(r["id"]) for r in csv.DictReader(fh)}
        with (out_dir / "orders.csv").open() as fh:
            order_fk_ids = {int(r["customer_id"]) for r in csv.DictReader(fh)}

        dangling = order_fk_ids - customer_ids
        assert not dangling, f"Dangling FK values in orders.customer_id: {dangling}"


# ---------------------------------------------------------------------------
# load subcommand (SQLite — fully offline)
# ---------------------------------------------------------------------------

class TestLoadSubcommand:

    def test_load_creates_rows_in_sqlite(self, schema_yaml, tmp_path):
        db_path = str(tmp_path / "test.db")
        main(["load", schema_yaml, "--dialect", "sqlite", "--dsn", db_path, "--sf", "0.1"])

        conn = sqlite3.connect(db_path)
        n_customers = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        n_orders    = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()

        assert n_customers == 10, f"expected 10 customers, got {n_customers}"
        assert n_orders    == 30, f"expected 30 orders, got {n_orders}"

    def test_load_is_reproducible_with_seed(self, schema_yaml, tmp_path):
        """Same seed must produce identical first rows."""
        def _first_row(db_path):
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT * FROM customers ORDER BY id LIMIT 1").fetchone()
            conn.close()
            return row

        db1, db2 = str(tmp_path / "a.db"), str(tmp_path / "b.db")
        main(["load", schema_yaml, "--dialect", "sqlite", "--dsn", db1, "--sf", "0.1", "--seed", "7"])
        main(["load", schema_yaml, "--dialect", "sqlite", "--dsn", db2, "--sf", "0.1", "--seed", "7"])

        assert _first_row(db1) == _first_row(db2)


# ---------------------------------------------------------------------------
# collect subcommand (SQLite — fully offline)
# ---------------------------------------------------------------------------

class TestCollectSubcommand:

    @pytest.fixture
    def sqlite_db(self, tmp_path: Path) -> str:
        """A minimal SQLite database with two real tables."""
        db_path = str(tmp_path / "source.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE products (
                product_id   INTEGER PRIMARY KEY,
                name         TEXT    NOT NULL,
                price        REAL    NOT NULL
            );
            CREATE TABLE reviews (
                review_id    INTEGER PRIMARY KEY,
                product_id   INTEGER NOT NULL,
                rating       INTEGER NOT NULL,
                body         TEXT
            );
            INSERT INTO products VALUES (1, 'Widget', 9.99);
            INSERT INTO products VALUES (2, 'Gadget', 29.99);
            INSERT INTO reviews  VALUES (1, 1, 5, 'Great!');
            INSERT INTO reviews  VALUES (2, 2, 3, NULL);
        """)
        conn.close()
        return db_path

    def test_collect_writes_schema_yaml(self, sqlite_db, tmp_path):
        schema_out = str(tmp_path / "schema.yaml")
        stats_out  = str(tmp_path / "stats.yaml")
        main([
            "collect",
            "--dialect", "sqlite",
            "--catalog", sqlite_db,
            "--tables",  "%",
            "--out-schema", schema_out,
            "--out-stats",  stats_out,
        ])

        assert Path(schema_out).exists(), "schema.yaml was not written"
        assert Path(stats_out).exists(),  "stats.yaml was not written"

        content = Path(schema_out).read_text()
        assert "products" in content
        assert "reviews"  in content

    def test_collect_schema_has_correct_columns(self, sqlite_db, tmp_path):
        schema_out = str(tmp_path / "schema.yaml")
        main([
            "collect",
            "--dialect", "sqlite",
            "--catalog", sqlite_db,
            "--tables",  "products",
            "--out-schema", schema_out,
            "--out-stats",  str(tmp_path / "stats.yaml"),
        ])

        import yaml as _yaml
        data = _yaml.safe_load(Path(schema_out).read_text())
        tables = {t["name"]: t for t in data["tables"]}
        col_names = [c["name"] for c in tables["products"]["columns"]]
        assert "product_id" in col_names
        assert "name"       in col_names
        assert "price"      in col_names


# ---------------------------------------------------------------------------
# inject subcommand — error path (no live DB required)
# ---------------------------------------------------------------------------

class TestInjectSubcommand:

    def test_unsupported_dialect_exits(self, tmp_path):
        """SQLite has no stats injection — inject must exit(1) cleanly."""
        # Build a minimal stats.yaml so the file-load step passes
        stats_yaml = tmp_path / "stats.yaml"
        stats_yaml.write_text("tables: []\n")

        with pytest.raises(SystemExit) as exc_info:
            main(["inject", "--dialect", "sqlite", "--stats", str(stats_yaml)])
        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# README Quick-start examples
# ---------------------------------------------------------------------------

class TestREADMEParseDDL:
    """Verify the DDL transpilation snippet from README works for all six targets."""

    _MYSQL_DDL = """
CREATE TABLE orders (
    order_id    INT           NOT NULL AUTO_INCREMENT,
    status      VARCHAR(20)   NOT NULL DEFAULT 'pending',
    total       DECIMAL(10,2)     NULL,
    is_paid     TINYINT(1)    NOT NULL DEFAULT 0,
    created_at  DATETIME          NULL,
    PRIMARY KEY (order_id)
) ENGINE=InnoDB;
"""

    @pytest.mark.parametrize("dialect", ["postgres", "oracle", "sqlserver", "databricks", "mysql", "db2"])
    def test_emit_ddl_dialect(self, dialect):
        from src.statschema.ddl_parser import parse_ddl
        from src.statschema.ddl_emitter import emit_ddl

        tables = parse_ddl(self._MYSQL_DDL, dialect="mysql")
        assert len(tables) == 1, "Expected exactly one table from the MySQL DDL"

        result = emit_ddl(tables[0], dialect)
        assert "CREATE TABLE" in result.upper()
        assert "orders" in result.lower()


class TestREADMEOrdersYAML:
    """Verify the orders / order_details generation example from README runs correctly."""

    @pytest.fixture(scope="class")
    def generated(self, tmp_path_factory):
        """Generate data once for all assertions (scale_factor=0.1 for speed)."""
        import yaml as _yaml
        from src.statschema.schema_io import load_canonical, resolve_load_order, resolve_row_counts
        from src.statschema.row_generator import generate_rows

        p = tmp_path_factory.mktemp("orders") / "orders_schema.yaml"
        p.write_text(_README_ORDERS_YAML)

        tables  = load_canonical(str(p))
        counts  = resolve_row_counts(tables, scale_factor=0.1)
        ordered = resolve_load_order(tables)

        result: dict[str, list[dict]] = {}
        for tbl in ordered:
            result[tbl.name] = list(generate_rows(tbl, counts[tbl.name], parent_row_counts=counts, seed=42))
        return result

    def test_row_counts(self, generated):
        # row_count_per_sf * scale_factor: 1000*0.1=100 orders, 3500*0.1=350 order_details
        assert len(generated["orders"])        == 100
        assert len(generated["order_details"]) == 350

    def test_fk_integrity(self, generated):
        """Every order_details.order_id must reference a real orders.order_id."""
        order_ids  = {r["order_id"] for r in generated["orders"]}
        detail_fks = {r["order_id"] for r in generated["order_details"]}
        dangling = detail_fks - order_ids
        assert not dangling, f"Dangling FK values: {dangling}"

    def test_status_distribution(self, generated):
        """'delivered' should be the most common status (weight 0.50)."""
        from collections import Counter
        counts = Counter(r["status"] for r in generated["orders"])
        most_common = counts.most_common(1)[0][0]
        assert most_common == "delivered"

    def test_conditional_nulls(self, generated):
        """discount_pct should be NULL approximately 65% of the time."""
        rows = generated["order_details"]
        null_rate = sum(1 for r in rows if r["discount_pct"] is None) / len(rows)
        # Allow ±15 percentage points of slack at scale_factor=0.1
        assert 0.50 <= null_rate <= 0.80, f"discount_pct null_rate {null_rate:.2%} outside expected range"

    def test_ship_date_partial_nulls(self, generated):
        """ship_date should be NULL ~8% of the time."""
        rows = generated["orders"]
        null_rate = sum(1 for r in rows if r["ship_date"] is None) / len(rows)
        assert 0.00 <= null_rate <= 0.25, f"ship_date null_rate {null_rate:.2%} unexpectedly high"
