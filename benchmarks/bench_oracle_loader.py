#!/usr/bin/env python3
"""
Oracle direct-path-load vs multi-row-insert benchmark.

Oracle's fast path is conn.direct_path_load() from python-oracledb — a
client-side bulk API that streams rows directly into Oracle's buffer cache
via the direct path protocol, bypassing redo logging for the data blocks.
No file staging is needed; unlike DB2 ADMIN_CMD LOAD, there is no dependency
on the /tmp/lima shared mount.

Quick mode  (--mode quick): 10 000 rows of order_line — smoke test.
Large mode  (--mode large): 300 000 rows of order_line (TPC-C sf=1).

Usage:
    python benchmarks/bench_oracle_loader.py --mode quick
    python benchmarks/bench_oracle_loader.py --mode large
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from benchmarks.tpc_generators import tpcc_rows
from benchmarks.tpc_schemas import TPCC_COLUMNS
from statschema.connection_profile import load_profile

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Schema / DDL
# ---------------------------------------------------------------------------

_BENCH_YAML = str(_REPO / "config" / "statschema.tpcb.yaml")
_p     = load_profile(_BENCH_YAML, "tpcb_oracle")
_SCHEMA = "bench_ora_load"
_USER   = _p.username or "system"
_PASS   = _p.password or ""
_HOST   = _p.host     or "127.0.0.1"
_PORT   = str(_p.port or 1521)
_SVC    = _p.database or "XE"
_DSN    = f"{_HOST}:{_PORT}/{_SVC}"

# Oracle DDL for order_line.  Uses Oracle-native types; no CLOB columns so
# direct_path_load works without restriction.
_ORDER_LINE_DDL = """\
CREATE TABLE order_line (
    ol_o_id        NUMBER(10)     NOT NULL,
    ol_d_id        NUMBER(10)     NOT NULL,
    ol_w_id        NUMBER(10)     NOT NULL,
    ol_number      NUMBER(10)     NOT NULL,
    ol_i_id        NUMBER(10)     NOT NULL,
    ol_supply_w_id NUMBER(10)     NOT NULL,
    ol_delivery_d  TIMESTAMP,
    ol_quantity    NUMBER(10)     NOT NULL,
    ol_amount      NUMBER(8,2)    NOT NULL,
    ol_dist_info   CHAR(24)       NOT NULL,
    CONSTRAINT pk_ol PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
)"""

_COL_NAMES  = TPCC_COLUMNS["order_line"]
_UPPER_COLS = [c.upper() for c in _COL_NAMES]

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect_oracle():
    import oracledb
    conn = oracledb.connect(user=_USER, password=_PASS, dsn=_DSN)
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------

def setup_schema(conn) -> None:
    cur = conn.cursor()
    # Create the user/schema if it doesn't exist
    try:
        cur.execute(f"CREATE USER {_SCHEMA} IDENTIFIED BY bench123 "
                    f"DEFAULT TABLESPACE users TEMPORARY TABLESPACE temp")
        cur.execute(f"GRANT CONNECT, RESOURCE, CREATE SESSION TO {_SCHEMA}")
        cur.execute(f"ALTER USER {_SCHEMA} QUOTA UNLIMITED ON users")
        conn.commit()
    except Exception:
        conn.rollback()  # already exists

    # Switch to that schema context
    cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {_SCHEMA.upper()}")

    # Drop + recreate table
    try:
        cur.execute(f"DROP TABLE {_SCHEMA.upper()}.ORDER_LINE PURGE")
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute(_ORDER_LINE_DDL)
    conn.commit()
    print(f"  schema {_SCHEMA!r} ready")


def truncate_table(conn) -> None:
    cur = conn.cursor()
    # TRUNCATE is DDL in Oracle — auto-commits, not logged for data blocks.
    cur.execute(f"TRUNCATE TABLE {_SCHEMA.upper()}.ORDER_LINE")
    # No explicit commit needed (DDL auto-commits), but harmless:
    conn.commit()


# ---------------------------------------------------------------------------
# Row generation
# ---------------------------------------------------------------------------

def generate_rows(n_rows: int | None) -> list[tuple]:
    gen = tpcc_rows("order_line", sf=1)
    if n_rows is not None:
        gen = itertools.islice(gen, n_rows)
    return list(gen)


# ---------------------------------------------------------------------------
# Load path A: direct_path_load (python-oracledb bulk API)
# ---------------------------------------------------------------------------

def _coerce_direct(row: tuple) -> tuple:
    """Coerce a raw tpcc_rows tuple for direct_path_load.

    direct_path_load requires:
    - NUMBER columns: Python int or float (not str)
    - CHAR/VARCHAR2: str
    - TIMESTAMP: datetime object (passed through)
    - NULL: None
    """
    return tuple(None if v is None else v for v in row)


def run_direct_path(conn, rows: list[tuple]) -> float:
    """Stream rows via conn.direct_path_load(). Returns elapsed seconds."""
    coerced = [_coerce_direct(r) for r in rows]

    t0 = time.perf_counter()
    conn.direct_path_load(
        schema_name=_SCHEMA.upper(),
        table_name="ORDER_LINE",
        column_names=_UPPER_COLS,
        data=coerced,
    )
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Load path B: parameterised multi-row INSERT (executemany, 500-row batches)
# ---------------------------------------------------------------------------

def run_multi_row(conn, rows: list[tuple]) -> float:
    """Parameterised executemany, 500-row batches. Returns elapsed seconds."""
    placeholders = ", ".join([f":{i+1}" for i in range(len(_COL_NAMES))])
    sql = f"INSERT INTO {_SCHEMA.upper()}.ORDER_LINE VALUES ({placeholders})"
    batch_size = 500  # Oracle default; _DIALECT_BATCH_DEFAULTS["oracle"].max_rows

    t0 = time.perf_counter()
    cur = conn.cursor()
    for i in range(0, len(rows), batch_size):
        cur.executemany(sql, rows[i : i + batch_size])
        conn.commit()
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(mode: str) -> None:
    n_rows = 10_000 if mode == "quick" else None

    print(f"\n=== Oracle loader benchmark — {mode} mode ===")
    print(f"  table: order_line (TPC-C sf=1"
          f"{'  [first 10 000 rows]' if mode == 'quick' else ''})")
    print(f"  fast path: direct_path_load (python-oracledb, no file staging)")

    print("  generating rows...", end=" ", flush=True)
    t0 = time.perf_counter()
    rows = generate_rows(n_rows)
    gen_s = time.perf_counter() - t0
    total = len(rows)
    print(f"{total:,} rows in {gen_s:.2f}s")

    print("  connecting to Oracle...", end=" ", flush=True)
    conn = connect_oracle()
    print("OK")

    setup_schema(conn)

    # ── Path A: direct_path_load ────────────────────────────────────────────
    print(f"\n[1/2] direct_path_load  (oracledb bulk protocol, no file I/O)")
    dp_s = run_direct_path(conn, rows)
    print(f"       total      : {dp_s:6.2f}s  ({total / dp_s:>9,.0f} rows/s)")

    truncate_table(conn)
    conn.close()
    conn = connect_oracle()
    conn.cursor().execute(f"ALTER SESSION SET CURRENT_SCHEMA = {_SCHEMA.upper()}")

    # ── Path B: multi-row INSERT ────────────────────────────────────────────
    print(f"\n[2/2] Multi-row INSERT  (executemany, 500-row batches)")
    multi_s = run_multi_row(conn, rows)
    print(f"       total      : {multi_s:6.2f}s  ({total / multi_s:>9,.0f} rows/s)")

    # ── Summary ─────────────────────────────────────────────────────────────
    speedup = multi_s / dp_s
    print(f"\n{'─' * 55}")
    print(f"  direct_path_load  : {dp_s:6.2f}s  {total/dp_s:>9,.0f} rows/s")
    print(f"  Multi-row INSERT  : {multi_s:6.2f}s  {total/multi_s:>9,.0f} rows/s")
    print(f"  Speedup: {speedup:.1f}× faster with direct_path_load" if speedup >= 1
          else f"  Multi-row is {1/speedup:.1f}× faster (direct_path slower)")
    print(f"{'─' * 55}\n")

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["quick", "large"], default="quick",
                    help="quick=10K rows (smoke test), large=300K rows (full sf=1)")
    args = ap.parse_args()
    run(args.mode)
