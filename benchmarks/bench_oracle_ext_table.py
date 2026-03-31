#!/usr/bin/env python3
"""
Oracle External Table load vs direct_path_load vs multi-row INSERT benchmark.

Oracle External Tables are Oracle's file-based bulk load path — analogous to
DB2 ADMIN_CMD LOAD.  The Oracle server process (uid=54321 inside the container)
reads a CSV directly from the /tmp/lima shared mount and the load is executed
as a single INSERT INTO target SELECT * FROM external_table statement.

Load paths compared:
  1. External Table (ORACLE_LOADER → INSERT ... SELECT)  — file on /tmp/lima
  2. direct_path_load (python-oracledb bulk wire protocol) — no file
  3. Multi-row INSERT (executemany, 500-row batches)       — no file

Requires: Oracle container started with -v /tmp/lima:/tmp/lima
  (update oracle.yaml + limactl restart oracle, or manually patch the
   container-oracle-xe.service ExecStart line)

Usage:
    python benchmarks/bench_oracle_ext_table.py --mode quick
    python benchmarks/bench_oracle_ext_table.py --mode large
"""

from __future__ import annotations

import argparse
import csv
import itertools
import logging
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from benchmarks.tpc_generators import tpcc_rows
from benchmarks.tpc_schemas import TPCC_COLUMNS

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCHEMA = "BENCH_ORA_LOAD"
_USER   = os.environ.get("ORACLE_USER", "system")
_PASS   = os.environ.get("ORACLE_PASS", "oracle")
_HOST   = os.environ.get("ORACLE_HOST", "127.0.0.1")
_PORT   = os.environ.get("ORACLE_PORT", "1521")
_SVC    = os.environ.get("ORACLE_SERVICE", "XE")
_DSN    = f"{_HOST}:{_PORT}/{_SVC}"

_STAGING = os.environ.get("STATSCHEMA_CLIENT_STAGING_DIR", "").strip()
if not _STAGING:
    raise RuntimeError(
        "STATSCHEMA_CLIENT_STAGING_DIR is not set. "
        "Set it to the shared filesystem path writable by statschema and "
        "readable by the Oracle server process. "
        "Example: STATSCHEMA_CLIENT_STAGING_DIR=/tmp/lima/statschema"
    )
_EXT_CSV      = "bench_ext_ol.csv"         # fixed name — external table DDL references it
_EXT_CSV_PATH = os.path.join(_STAGING, _EXT_CSV)

_COL_NAMES  = TPCC_COLUMNS["order_line"]
_UPPER_COLS = [c.upper() for c in _COL_NAMES]

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_TARGET_DDL = """\
CREATE TABLE {schema}.ORDER_LINE (
    ol_o_id        NUMBER(10)   NOT NULL,
    ol_d_id        NUMBER(10)   NOT NULL,
    ol_w_id        NUMBER(10)   NOT NULL,
    ol_number      NUMBER(10)   NOT NULL,
    ol_i_id        NUMBER(10)   NOT NULL,
    ol_supply_w_id NUMBER(10)   NOT NULL,
    ol_delivery_d  TIMESTAMP,
    ol_quantity    NUMBER(10)   NOT NULL,
    ol_amount      NUMBER(8,2)  NOT NULL,
    ol_dist_info   CHAR(24)     NOT NULL,
    CONSTRAINT pk_ol_{schema} PRIMARY KEY
        (ol_w_id, ol_d_id, ol_o_id, ol_number)
)"""

# ORACLE_LOADER external table.
# - MISSING FIELD VALUES ARE NULL handles the empty fields written for None.
# - ol_delivery_d is a CHAR in the external table; we cast it to TIMESTAMP
#   in the INSERT ... SELECT using TO_TIMESTAMP so Oracle doesn't try to apply
#   a mask to empty strings (which would error even with MISSING FIELD VALUES).
_EXT_DDL = """\
CREATE TABLE {schema}.EXT_ORDER_LINE (
    ol_o_id        NUMBER(10),
    ol_d_id        NUMBER(10),
    ol_w_id        NUMBER(10),
    ol_number      NUMBER(10),
    ol_i_id        NUMBER(10),
    ol_supply_w_id NUMBER(10),
    ol_delivery_d  VARCHAR2(30),
    ol_quantity    NUMBER(10),
    ol_amount      NUMBER(8,2),
    ol_dist_info   CHAR(24)
)
ORGANIZATION EXTERNAL (
    TYPE ORACLE_LOADER
    DEFAULT DIRECTORY bench_ext_dir
    ACCESS PARAMETERS (
        RECORDS DELIMITED BY NEWLINE
        CHARACTERSET AL32UTF8
        LOGFILE  bench_ext_log_dir:'ext_ol.log'
        BADFILE  bench_ext_log_dir:'ext_ol.bad'
        DISCARDFILE bench_ext_log_dir:'ext_ol.dsc'
        FIELDS TERMINATED BY ','
        OPTIONALLY ENCLOSED BY '"'
        MISSING FIELD VALUES ARE NULL
    )
    LOCATION ('{csv}')
)
REJECT LIMIT UNLIMITED"""

# Cast ol_delivery_d VARCHAR2 → TIMESTAMP at INSERT time.
_INSERT_FROM_EXT = """\
INSERT INTO {schema}.ORDER_LINE
SELECT
    ol_o_id, ol_d_id, ol_w_id, ol_number, ol_i_id, ol_supply_w_id,
    CASE WHEN ol_delivery_d IS NULL THEN NULL
         ELSE TO_TIMESTAMP(ol_delivery_d, 'YYYY-MM-DD HH24:MI:SS')
    END,
    ol_quantity, ol_amount, ol_dist_info
FROM {schema}.EXT_ORDER_LINE"""


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

def _exec_ignore(cur, sql: str, conn=None) -> None:
    try:
        cur.execute(sql)
        if conn:
            conn.commit()
    except Exception:
        if conn:
            conn.rollback()


def setup_schema(conn) -> None:
    cur = conn.cursor()

    # Ensure user/schema exists
    _exec_ignore(cur, (
        f"CREATE USER {_SCHEMA} IDENTIFIED BY bench123 "
        f"DEFAULT TABLESPACE users TEMPORARY TABLESPACE temp"
    ), conn)
    _exec_ignore(cur, f"GRANT CONNECT, RESOURCE, CREATE SESSION TO {_SCHEMA}", conn)
    _exec_ignore(cur, f"ALTER USER {_SCHEMA} QUOTA UNLIMITED ON users", conn)

    # DIRECTORY objects:
    #   bench_ext_dir     — data files on the 9p-shared /tmp/lima mount (read-only for oracle)
    #   bench_ext_log_dir — log/bad/discard files; /tmp inside the container is world-writable
    cur.execute(f"CREATE OR REPLACE DIRECTORY bench_ext_dir AS '{_STAGING}'")
    cur.execute(f"GRANT READ ON DIRECTORY bench_ext_dir TO {_SCHEMA}")
    cur.execute("CREATE OR REPLACE DIRECTORY bench_ext_log_dir AS '/tmp'")
    cur.execute(f"GRANT READ, WRITE ON DIRECTORY bench_ext_log_dir TO {_SCHEMA}")
    conn.commit()

    # Drop and recreate target + external tables
    for t in ("ORDER_LINE", "EXT_ORDER_LINE"):
        _exec_ignore(cur, f"DROP TABLE {_SCHEMA}.{t} PURGE", conn)

    cur.execute(_TARGET_DDL.format(schema=_SCHEMA))
    conn.commit()

    cur.execute(_EXT_DDL.format(schema=_SCHEMA, csv=_EXT_CSV))
    conn.commit()

    print(f"  schema {_SCHEMA!r} ready  (DIRECTORY bench_ext_dir → {_STAGING})")


def truncate_table(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE {_SCHEMA}.ORDER_LINE")
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
# Path A: External Table  (write CSV → /tmp/lima, INSERT ... SELECT)
# ---------------------------------------------------------------------------

def write_csv(rows: list[tuple]) -> float:
    """Write rows to the fixed CSV path. Returns elapsed seconds."""
    os.makedirs(_STAGING, exist_ok=True)
    t0 = time.perf_counter()
    with open(_EXT_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
    # chmod 0o644: oracle (uid=54321) is not the macOS file owner (uid=502).
    # External table loader needs at least world-read permission.
    os.chmod(_EXT_CSV_PATH, 0o644)
    return time.perf_counter() - t0


def run_ext_table(conn, rows: list[tuple]) -> tuple[float, float]:
    """
    Write CSV to /tmp/lima then INSERT INTO target SELECT * FROM ext table.
    Returns (write_s, load_s).
    """
    write_s = write_csv(rows)

    t0 = time.perf_counter()
    cur = conn.cursor()
    cur.execute(_INSERT_FROM_EXT.format(schema=_SCHEMA))
    conn.commit()
    load_s = time.perf_counter() - t0

    return write_s, load_s


# ---------------------------------------------------------------------------
# Path B: direct_path_load (python-oracledb bulk wire protocol)
# ---------------------------------------------------------------------------

def run_direct_path(conn, rows: list[tuple]) -> float:
    t0 = time.perf_counter()
    conn.direct_path_load(
        schema_name=_SCHEMA,
        table_name="ORDER_LINE",
        column_names=_UPPER_COLS,
        data=[tuple(None if v is None else v for v in r) for r in rows],
    )
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Path C: multi-row INSERT (executemany, 500-row batches)
# ---------------------------------------------------------------------------

def run_multi_row(conn, rows: list[tuple]) -> float:
    placeholders = ", ".join([f":{i+1}" for i in range(len(_COL_NAMES))])
    sql = f"INSERT INTO {_SCHEMA}.ORDER_LINE VALUES ({placeholders})"
    batch_size = 500

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

    print(f"\n=== Oracle loader benchmark (3 paths) — {mode} mode ===")
    print(f"  table: order_line (TPC-C sf=1"
          f"{'  [first 10 000 rows]' if mode == 'quick' else ''})")

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

    # ── Path A: External Table ──────────────────────────────────────────────
    print(f"\n[1/3] External Table  (write CSV → /tmp/lima 9p, INSERT...SELECT)")
    write_s, load_s = run_ext_table(conn, rows)
    ext_total_s = write_s + load_s
    print(f"       file write : {write_s:6.2f}s  ({total / write_s:>9,.0f} rows/s)")
    print(f"       INSERT/SEL : {load_s:6.2f}s  ({total / load_s:>9,.0f} rows/s)")
    print(f"       total      : {ext_total_s:6.2f}s  ({total / ext_total_s:>9,.0f} rows/s)")

    truncate_table(conn)
    conn.close()
    conn = connect_oracle()

    # ── Path B: direct_path_load ────────────────────────────────────────────
    print(f"\n[2/3] direct_path_load  (oracledb bulk wire protocol, no file I/O)")
    dp_s = run_direct_path(conn, rows)
    print(f"       total      : {dp_s:6.2f}s  ({total / dp_s:>9,.0f} rows/s)")

    truncate_table(conn)
    conn.close()
    conn = connect_oracle()

    # ── Path C: multi-row INSERT ────────────────────────────────────────────
    print(f"\n[3/3] Multi-row INSERT  (executemany, 500-row batches)")
    multi_s = run_multi_row(conn, rows)
    print(f"       total      : {multi_s:6.2f}s  ({total / multi_s:>9,.0f} rows/s)")

    # ── Summary ─────────────────────────────────────────────────────────────
    baseline = multi_s
    print(f"\n{'─' * 60}")
    print(f"  External Table (total)  : {ext_total_s:6.2f}s  {total/ext_total_s:>9,.0f} rows/s"
          f"  {baseline/ext_total_s:.1f}× vs multi-row")
    print(f"  direct_path_load        : {dp_s:6.2f}s  {total/dp_s:>9,.0f} rows/s"
          f"  {baseline/dp_s:.1f}× vs multi-row")
    print(f"  Multi-row INSERT        : {multi_s:6.2f}s  {total/multi_s:>9,.0f} rows/s"
          f"  1.0× (baseline)")
    print(f"{'─' * 60}\n")

    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["quick", "large"], default="quick",
                    help="quick=10K rows (smoke test), large=300K rows (full sf=1)")
    args = ap.parse_args()
    run(args.mode)
