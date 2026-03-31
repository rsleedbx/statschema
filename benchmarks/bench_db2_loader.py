#!/usr/bin/env python3
"""
DB2 bulk-load vs multi-row-insert benchmark.

Quick mode  (--mode quick): 10 000 rows of order_line — verifies both paths
                             execute without error and gives a first timing.
Large mode  (--mode large): 300 000 rows of order_line (TPC-C sf=1) — the
                            largest single table in the standard TPC-C workload.

Usage:
    python benchmarks/bench_db2_loader.py --mode quick
    python benchmarks/bench_db2_loader.py --mode large
"""

from __future__ import annotations

import argparse
import csv
import itertools
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from benchmarks.tpc_generators import tpcc_rows
from benchmarks.tpc_schemas import TPCC_COLUMNS

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Schema / DDL
# ---------------------------------------------------------------------------

_SCHEMA = "bench_db2_load"

# DB2 DDL for order_line — no CLOB columns, exercises ADMIN_CMD DEL format fully.
_ORDER_LINE_DDL = """\
CREATE TABLE order_line (
    ol_o_id        INTEGER       NOT NULL,
    ol_d_id        INTEGER       NOT NULL,
    ol_w_id        INTEGER       NOT NULL,
    ol_number      INTEGER       NOT NULL,
    ol_i_id        INTEGER       NOT NULL,
    ol_supply_w_id INTEGER       NOT NULL,
    ol_delivery_d  TIMESTAMP,
    ol_quantity    INTEGER       NOT NULL,
    ol_amount      DECIMAL(6,2)  NOT NULL,
    ol_dist_info   CHAR(24)      NOT NULL,
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
)"""

_COL_NAMES = TPCC_COLUMNS["order_line"]

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect_db2():
    import ibm_db_dbi  # type: ignore
    host = os.environ.get("DB2_HOST",     "127.0.0.1")
    port = os.environ.get("DB2_PORT",     "50000")
    db   = os.environ.get("DB2_DATABASE", "testdb")
    user = os.environ.get("DB2_USER",     "db2inst1")
    pwd  = os.environ.get("DB2_PASS",     "testpass")
    dsn  = f"DATABASE={db};HOSTNAME={host};PORT={port};PROTOCOL=TCPIP;UID={user};PWD={pwd};"
    return ibm_db_dbi.connect(dsn, "", "")


# ---------------------------------------------------------------------------
# Schema lifecycle
# ---------------------------------------------------------------------------

def setup_schema(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT TABNAME FROM SYSCAT.TABLES WHERE TABSCHEMA = '{_SCHEMA.upper()}'"
        )
        for (tab,) in cur.fetchall():
            try:
                cur.execute(f"DROP TABLE {_SCHEMA}.{tab}")
            except Exception:
                pass
    except Exception:
        pass
    try:
        cur.execute(f"DROP SCHEMA {_SCHEMA} RESTRICT")
    except Exception:
        pass
    cur.execute(f"CREATE SCHEMA {_SCHEMA}")
    conn.commit()
    cur.execute(f"SET SCHEMA {_SCHEMA}")
    cur.execute(_ORDER_LINE_DDL)
    conn.commit()
    print(f"  schema {_SCHEMA!r} ready")


def truncate_table(conn) -> None:
    # DELETE is fully logged and overflows the transaction log at 300K rows.
    # TRUNCATE TABLE ... IMMEDIATE is non-logged (DB2 equivalent of TRUNCATE).
    cur = conn.cursor()
    cur.execute(f'TRUNCATE TABLE "{_SCHEMA.upper()}"."ORDER_LINE" IMMEDIATE')
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
# Load path A: write CSV to shared filesystem, call ADMIN_CMD LOAD
# ---------------------------------------------------------------------------

_STAGING = os.environ.get("STATSCHEMA_CLIENT_STAGING_DIR", "").strip()
if not _STAGING:
    raise RuntimeError(
        "STATSCHEMA_CLIENT_STAGING_DIR is not set. "
        "Set it to the shared filesystem path writable by statschema and "
        "readable by the DB2 server process. "
        "Example: STATSCHEMA_CLIENT_STAGING_DIR=/tmp/lima/statschema"
    )


def run_admin_cmd(conn, rows: list[tuple]) -> tuple[float, float]:
    """
    Returns (write_s, load_s):
      write_s — time to write the DEL file to /tmp/lima (macOS → 9p)
      load_s  — time for ADMIN_CMD LOAD (DB ingestion, after file is ready)
    """
    os.makedirs(_STAGING, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="bench_ol_", suffix=".del", dir=_STAGING)

    t_write_start = time.perf_counter()
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    # mkstemp creates 0o600; DB2 runs as db2inst1 (uid 1000) which is not the
    # macOS file owner, so it needs at least world-read permission to load the file.
    os.chmod(path, 0o644)
    write_s = time.perf_counter() - t_write_start

    full = f'"{_SCHEMA.upper()}"."ORDER_LINE"'
    t_load_start = time.perf_counter()
    try:
        cur = conn.cursor()
        cur.execute(
            f"CALL SYSPROC.ADMIN_CMD("
            f"'LOAD FROM {path} OF DEL INSERT INTO {full} NONRECOVERABLE')"
        )
        conn.commit()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    load_s = time.perf_counter() - t_load_start

    return write_s, load_s


# ---------------------------------------------------------------------------
# Load path B: parameterised multi-row INSERT (executemany, 1000-row batches)
# ---------------------------------------------------------------------------

def run_multi_row(conn, rows: list[tuple]) -> float:
    """Returns total elapsed seconds for all batches + commit."""
    n_cols = len(_COL_NAMES)
    placeholders = ", ".join(["?"] * n_cols)
    sql = f'INSERT INTO "{_SCHEMA.upper()}"."ORDER_LINE" VALUES ({placeholders})'
    batch_size = 1000

    t0 = time.perf_counter()
    cur = conn.cursor()
    for i in range(0, len(rows), batch_size):
        cur.executemany(sql, rows[i : i + batch_size])
        conn.commit()  # commit per batch — a single transaction for 300K rows fills the 52MB log
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(mode: str) -> None:
    n_rows = 10_000 if mode == "quick" else None  # None → full sf=1 (300 000 rows)

    print(f"\n=== DB2 loader benchmark — {mode} mode ===")
    print(f"  table: order_line (TPC-C sf=1{'  [first 10 000 rows]' if mode == 'quick' else ''})")

    print("  generating rows...", end=" ", flush=True)
    t0 = time.perf_counter()
    rows = generate_rows(n_rows)
    gen_s = time.perf_counter() - t0
    total = len(rows)
    print(f"{total:,} rows in {gen_s:.2f}s")

    print("  connecting to DB2...", end=" ", flush=True)
    conn = connect_db2()
    print("OK")

    setup_schema(conn)

    # ── Path A: ADMIN_CMD LOAD ──────────────────────────────────────────────
    print(f"\n[1/2] ADMIN_CMD LOAD  (write CSV → /tmp/lima 9p, then LOAD)")
    write_s, load_s = run_admin_cmd(conn, rows)
    total_admin_s = write_s + load_s
    print(f"       file write : {write_s:6.2f}s  ({total / write_s:>9,.0f} rows/s)")
    print(f"       LOAD cmd   : {load_s:6.2f}s  ({total / load_s:>9,.0f} rows/s)")
    print(f"       total      : {total_admin_s:6.2f}s  ({total / total_admin_s:>9,.0f} rows/s)")

    truncate_table(conn)
    conn.close()
    conn = connect_db2()

    # ── Path B: multi-row INSERT ────────────────────────────────────────────
    print(f"\n[2/2] Multi-row INSERT  (executemany, 1000-row batches)")
    multi_s = run_multi_row(conn, rows)
    print(f"       total      : {multi_s:6.2f}s  ({total / multi_s:>9,.0f} rows/s)")

    # ── Summary ─────────────────────────────────────────────────────────────
    speedup = multi_s / total_admin_s
    print(f"\n{'─' * 55}")
    print(f"  ADMIN_CMD LOAD (total) : {total_admin_s:6.2f}s  {total/total_admin_s:>9,.0f} rows/s")
    print(f"  Multi-row INSERT       : {multi_s:6.2f}s  {total/multi_s:>9,.0f} rows/s")
    print(f"  Speedup: {speedup:.1f}× faster with ADMIN_CMD LOAD" if speedup >= 1
          else f"  Multi-row is {1/speedup:.1f}× faster (ADMIN_CMD slower)")
    print(f"{'─' * 55}\n")

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["quick", "large"], default="quick",
                    help="quick=10K rows (verify), large=300K rows (full sf=1)")
    args = ap.parse_args()
    run(args.mode)
