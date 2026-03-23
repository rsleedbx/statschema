"""
TPC-E workload validation.

Generates all 32 TPC-E tables from the canonical YAML at SF=0.01, loads
them into an in-memory DuckDB database, then runs representative SELECT
queries for each of the 10 TPC-E transaction types.

TPC-E transaction types covered
--------------------------------
1.  Broker-Volume     — broker commission totals by industry sector
2.  Customer-Position — customer account balances and current holdings value
3.  Market-Feed       — last trade price lookup by symbol
4.  Market-Watch      — portfolio value delta for a customer's watch list
5.  Security-Detail   — full security profile with company and address chain
6.  Trade-Lookup      — historical trade records (4 frame variants)
7.  Trade-Order       — customer account, broker, and security validation
8.  Trade-Result      — trade settlement and cash transaction recording
9.  Trade-Status      — recent trade status with type and security names
10. Trade-Update      — settlement and cash transaction retrieval

Validation criteria
-------------------
- Every query must execute without a SQL error.
- Every query must return at least one result row.

Runtime
-------
~20–35 seconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pandas")

import duckdb
import pandas as pd

from src.statschema.schema_io import load_canonical, resolve_row_counts, resolve_load_order
from src.statschema.row_generator import generate_rows

SCHEMA_PATH = Path(__file__).parent.parent / "benchmarks" / "schemas" / "tpce_schema.yaml"
SF = 0.01

# ─────────────────────────────────────────────────────────────────────────────
# Representative queries for each of the 10 TPC-E transaction types.
# These are the "Frame 1" SELECT portions of each transaction; write/UPDATE
# frames are omitted as they require transactional state unavailable offline.
# Column/table names match the tpce_schema.yaml canonical definitions.
# ─────────────────────────────────────────────────────────────────────────────

TPCE_QUERIES: list[tuple[str, str]] = [
    # 1. Broker-Volume: total bid-price-weighted volume per broker, by sector
    ("Broker-Volume",
     """
     SELECT b.b_name, SUM(tr.tr_qty * tr.tr_bid_price) AS volume
     FROM   trade_request tr
     JOIN   broker         b  ON b.b_id = tr.tr_b_id
     JOIN   security       s  ON s.s_symb = tr.tr_s_symb
     JOIN   company        co ON co.co_id = s.s_co_id
     JOIN   industry       i  ON i.in_id = co.co_in_id
     JOIN   sector         sc ON sc.sc_id = i.in_sc_id
     GROUP  BY b.b_name
     ORDER  BY volume DESC
     LIMIT  10
     """),

    # 2. Customer-Position: account balance plus current market value of holdings
    ("Customer-Position",
     """
     SELECT ca.ca_id, ca.ca_bal,
            COALESCE(SUM(hs.hs_qty * lt.lt_price), 0) AS market_value
     FROM   customer_account ca
     LEFT JOIN holding_summary hs ON hs.hs_ca_id = ca.ca_id
     LEFT JOIN last_trade      lt ON lt.lt_s_symb = hs.hs_s_symb
     GROUP  BY ca.ca_id, ca.ca_bal
     LIMIT  10
     """),

    # 3. Market-Feed: current last-trade price lookup by symbol
    ("Market-Feed",
     """
     SELECT lt.lt_s_symb, lt.lt_price, lt.lt_open_price, lt.lt_vol
     FROM   last_trade lt
     JOIN   security   s ON s.s_symb = lt.lt_s_symb
     ORDER  BY lt.lt_s_symb
     LIMIT  10
     """),

    # 4. Market-Watch: securities on a customer's watch list with their current price
    # (watch_item stores only symbol + creation_date; price comes from last_trade)
    ("Market-Watch",
     """
     SELECT wl.wl_c_id, wi.wi_s_symb,
            lt.lt_price AS current_price,
            dm.dm_close AS prev_close,
            (lt.lt_price - dm.dm_close) AS delta
     FROM   watch_list wl
     JOIN   watch_item   wi ON wi.wi_wl_id  = wl.wl_id
     JOIN   last_trade   lt ON lt.lt_s_symb = wi.wi_s_symb
     JOIN   daily_market dm ON dm.dm_s_symb = wi.wi_s_symb
     LIMIT  10
     """),

    # 5. Security-Detail: security profile through company → exchange → address
    # (address → zip_code join omitted: ad_zc_code has no FK to zip_code.zc_code)
    ("Security-Detail",
     """
     SELECT s.s_name, s.s_issue, s.s_ex_id,
            co.co_name, co.co_desc,
            ex.ex_name,
            a.ad_line1, a.ad_ctry
     FROM   security s
     JOIN   company  co ON co.co_id = s.s_co_id
     JOIN   exchange ex ON ex.ex_id = s.s_ex_id
     JOIN   address   a ON a.ad_id  = co.co_ad_id
     LIMIT  10
     """),

    # 6a. Trade-Lookup (frame 1): trade info + trade type for recent trades
    ("Trade-Lookup-F1",
     """
     SELECT t.t_id, t.t_dts, t.t_st_id, t.t_tt_id,
            t.t_s_symb, t.t_qty, t.t_bid_price, t.t_exec_name,
            t.t_is_cash, t.t_trade_price,
            tt.tt_name, tt.tt_is_mrkt
     FROM   trade      t
     JOIN   trade_type tt ON tt.tt_id = t.t_tt_id
     ORDER  BY t.t_dts DESC
     LIMIT  10
     """),

    # 6b. Trade-Lookup (frame 2): trade history for a set of trades
    ("Trade-Lookup-F2",
     """
     SELECT t.t_id, th.th_dts, th.th_st_id, st.st_name
     FROM   trade         t
     JOIN   trade_history th ON th.th_t_id = t.t_id
     JOIN   status_type   st ON st.st_id   = th.th_st_id
     ORDER  BY t.t_id, th.th_dts
     LIMIT  20
     """),

    # 6c. Trade-Lookup (frame 3): trade settlement and cash details
    ("Trade-Lookup-F3",
     """
     SELECT t.t_id, t.t_dts, t.t_qty, t.t_trade_price,
            ct.ct_amt, ct.ct_dts, ct.ct_name
     FROM   trade            t
     JOIN   cash_transaction ct ON ct.ct_t_id = t.t_id
     LIMIT  10
     """),

    # 7. Trade-Order: validate account, broker, and security before placing
    ("Trade-Order",
     """
     SELECT ca.ca_id, ca.ca_name, ca.ca_bal,
            b.b_name, b.b_id,
            s.s_name, s.s_symb, s.s_ex_id,
            lt.lt_price AS last_price
     FROM   customer_account ca
     JOIN   broker     b  ON b.b_id      = ca.ca_b_id
     JOIN   last_trade lt ON lt.lt_s_symb = (SELECT s_symb FROM security LIMIT 1)
     JOIN   security   s  ON s.s_symb    = lt.lt_s_symb
     LIMIT  10
     """),

    # 8. Trade-Result: completed trades joined to their account and security
    # (all generated trades have t_st_id='CMPT'; filter directly to avoid
    #  non-determinism from SELECT st_id FROM status_type LIMIT 1)
    ("Trade-Result",
     """
     SELECT t.t_id, t.t_dts, t.t_st_id, t.t_tt_id,
            t.t_s_symb, t.t_qty, t.t_trade_price, t.t_chrg,
            ca.ca_id, ca.ca_bal,
            s.s_name
     FROM   trade            t
     JOIN   customer_account ca ON ca.ca_id = t.t_ca_id
     JOIN   security          s ON s.s_symb = t.t_s_symb
     WHERE  t.t_st_id = 'CMPT'
     LIMIT  10
     """),

    # 9. Trade-Status: 50 most-recent trades for an account with full status/type names
    ("Trade-Status",
     """
     SELECT t.t_id, t.t_dts, st.st_name, tt.tt_name,
            t.t_s_symb, t.t_qty, t.t_exec_name, t.t_chrg,
            s.s_name, ex.ex_name
     FROM   trade       t
     JOIN   status_type st ON st.st_id   = t.t_st_id
     JOIN   trade_type  tt ON tt.tt_id   = t.t_tt_id
     JOIN   security     s ON s.s_symb   = t.t_s_symb
     JOIN   exchange    ex ON ex.ex_id   = s.s_ex_id
     ORDER  BY t.t_dts DESC
     LIMIT  50
     """),

    # 10. Trade-Update: cash transaction details for completed trades
    ("Trade-Update",
     """
     SELECT t.t_id, t.t_exec_name, t.t_trade_price,
            ct.ct_amt, ct.ct_name, ct.ct_dts
     FROM   trade            t
     JOIN   cash_transaction ct ON ct.ct_t_id = t.t_id
     JOIN   status_type      st ON st.st_id   = t.t_st_id
     ORDER  BY t.t_dts DESC
     LIMIT  10
     """),

    # Supplemental: Holding-History — track of historical position changes
    ("Holding-History",
     """
     SELECT hh.hh_t_id, hh.hh_h_t_id, hh.hh_before_qty, hh.hh_after_qty,
            t.t_dts, t.t_s_symb, t.t_qty
     FROM   holding_history hh
     JOIN   trade            t ON t.t_id = hh.hh_t_id
     LIMIT  10
     """),

    # Supplemental: Financial — company earnings time-series
    ("Financial",
     """
     SELECT f.fi_year, f.fi_qtr, f.fi_revenue, f.fi_net_earn,
            co.co_name, co.co_desc
     FROM   financial f
     JOIN   company   co ON co.co_id = f.fi_co_id
     ORDER  BY co.co_name, f.fi_year, f.fi_qtr
     LIMIT  20
     """),

    # Supplemental: News-Xref — news items via company (nx_co_id is the FK, not security)
    ("News-Xref",
     """
     SELECT ni.ni_headline, ni.ni_dts, ni.ni_source,
            co.co_name,
            s.s_name, s.s_symb
     FROM   news_xref nx
     JOIN   news_item  ni ON ni.ni_id  = nx.nx_ni_id
     JOIN   company    co ON co.co_id  = nx.nx_co_id
     JOIN   security    s ON s.s_co_id = co.co_id
     ORDER  BY ni.ni_dts DESC
     LIMIT  10
     """),
]


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: load all 32 TPC-E tables into an in-memory DuckDB connection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def duckdb_tpce():
    tables  = load_canonical(SCHEMA_PATH)
    counts  = resolve_row_counts(tables, scale_factor=SF)
    ordered = resolve_load_order(tables)

    conn = duckdb.connect()

    for tbl in ordered:
        n = counts.get(tbl.name, 0)
        if n == 0:
            continue
        rows = list(generate_rows(tbl, n, parent_row_counts=counts))
        if not rows:
            continue
        df = pd.DataFrame(rows)
        conn.register(f"_df_{tbl.name}", df)

        date_cols = {c.name for c in tbl.columns
                     if c.type and "date" in c.type.lower()
                     and "update" not in c.name.lower()}
        ts_cols   = {c.name for c in tbl.columns
                     if c.type and "timestamp" in c.type.lower()}
        select_parts = []
        for col_name in df.columns:
            if col_name in date_cols:
                select_parts.append(f"TRY_CAST({col_name} AS DATE) AS {col_name}")
            elif col_name in ts_cols:
                select_parts.append(f"TRY_CAST({col_name} AS TIMESTAMP) AS {col_name}")
            else:
                select_parts.append(col_name)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {tbl.name} AS "
            f"SELECT {', '.join(select_parts)} FROM _df_{tbl.name}"
        )

    return conn


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_tpce_table_row_counts(duckdb_tpce):
    """Every table must be non-empty after generation at SF=0.01."""
    conn = duckdb_tpce
    tables = load_canonical(SCHEMA_PATH)
    counts = resolve_row_counts(tables, scale_factor=SF)

    empty = []
    for tbl in tables:
        expected = counts.get(tbl.name, 0)
        if expected == 0:
            continue
        actual = conn.execute(f"SELECT COUNT(*) FROM {tbl.name}").fetchone()[0]
        if actual == 0:
            empty.append(f"{tbl.name} (expected {expected})")

    assert not empty, f"Tables generated 0 rows: {empty}"


@pytest.mark.parametrize("label,sql", TPCE_QUERIES)
def test_tpce_transaction_query(duckdb_tpce, label, sql):
    """Each TPC-E transaction query must execute without error and return rows."""
    conn = duckdb_tpce
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as e:
        pytest.fail(f"{label}: SQL error — {e}")
    assert rows, f"{label}: query returned 0 rows (FK chain broken or table empty)"
