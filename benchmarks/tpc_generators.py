"""
Streaming row generators for TPC-C and TPC-H benchmark data.

Design goals
------------
* Zero dependencies beyond the standard library — no Faker, no Spark, no pandas.
* Streaming: every generator is a Python generator (yield).  TPC-H LINEITEM
  at SF=1 produces 6 million rows without ever materialising more than one
  batch in memory.
* Reproducible: seeded with random.Random(seed) so the same SF always produces
  the same byte sequence, making load timings comparable across runs.
* FK-safe: child tables reference parent keys using modulo arithmetic, so the
  generated data is referentially consistent without needing to store parent
  keys in memory.

Usage
-----
    from benchmarks.tpc_generators import tpcc_rows, tpch_rows

    for row in tpch_rows("lineitem", sf=1):
        ...   # tuple of 16 columns, streaming

Row tuples are in the column order defined in benchmarks.tpc_schemas.
"""

from __future__ import annotations

import random
import string
from datetime import date, datetime, timedelta
from typing import Iterator

# ---------------------------------------------------------------------------
# Helpers shared by both schemas
# ---------------------------------------------------------------------------

_ALPHA = string.ascii_uppercase
_ALNUM = string.ascii_letters + string.digits


def _rstr(rng: random.Random, length: int, chars: str = _ALNUM) -> str:
    return "".join(rng.choices(chars, k=length))


def _rdate(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, delta))


def _rdatetime(rng: random.Random) -> datetime:
    d = _rdate(rng, date(2020, 1, 1), date(2023, 12, 31))
    return datetime(d.year, d.month, d.day,
                    rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))


def _rdec(rng: random.Random, lo: float, hi: float, scale: int) -> float:
    """Return a float rounded to `scale` decimal places — accepted by all DBAPI2 drivers."""
    val = lo + rng.random() * (hi - lo)
    return round(val, scale)


# ---------------------------------------------------------------------------
# TPC-C generators
# ---------------------------------------------------------------------------

def _tpcc_item(rng: random.Random, n_items: int) -> Iterator[tuple]:
    for i in range(1, n_items + 1):
        yield (
            i,                                      # i_id
            rng.randint(1, 10_000),                 # i_im_id
            _rstr(rng, 14),                         # i_name
            _rdec(rng, 1.00, 99.99, 2),             # i_price
            _rstr(rng, 26),                         # i_data
        )


def _tpcc_warehouse(rng: random.Random, w: int) -> Iterator[tuple]:
    for wid in range(1, w + 1):
        yield (
            wid,
            _rstr(rng, 6),                          # w_name
            _rstr(rng, 12),                         # w_street_1
            _rstr(rng, 12),                         # w_street_2
            _rstr(rng, 12),                         # w_city
            _rstr(rng, 2, _ALPHA),                  # w_state
            _rstr(rng, 9, string.digits),            # w_zip
            _rdec(rng, 0.0, 0.2, 4),                # w_tax
            300000.0,                   # w_ytd
        )


def _tpcc_district(rng: random.Random, w: int, d_per_w: int) -> Iterator[tuple]:
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            yield (
                did, wid,
                _rstr(rng, 6),
                _rstr(rng, 12), _rstr(rng, 12), _rstr(rng, 12),
                _rstr(rng, 2, _ALPHA),
                _rstr(rng, 9, string.digits),
                _rdec(rng, 0.0, 0.2, 4),
                30000.0,
                3001,                               # d_next_o_id
            )


def _tpcc_customer(rng: random.Random, w: int, d_per_w: int,
                   c_per_d: int) -> Iterator[tuple]:
    since_base = date(2020, 1, 1)
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            for cid in range(1, c_per_d + 1):
                yield (
                    cid, did, wid,
                    _rstr(rng, 8),                  # c_first
                    "OE",                           # c_middle
                    _rstr(rng, 8),                  # c_last
                    _rstr(rng, 12), _rstr(rng, 12), # street 1, 2
                    _rstr(rng, 12),                 # city
                    _rstr(rng, 2, _ALPHA),           # state
                    _rstr(rng, 9, string.digits),    # zip
                    _rstr(rng, 16, string.digits),   # phone
                    _rdatetime(rng),                 # c_since
                    rng.choice(["GC", "BC"]),        # c_credit
                    50000.0,             # c_credit_lim
                    _rdec(rng, 0.0, 0.5, 4),         # c_discount
                    -10.0,               # c_balance
                    10.0,                # c_ytd_payment
                    1,                              # c_payment_cnt
                    0,                              # c_delivery_cnt
                    _rstr(rng, 300),                # c_data
                )


def _tpcc_history(rng: random.Random, w: int, d_per_w: int,
                  c_per_d: int) -> Iterator[tuple]:
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            for cid in range(1, c_per_d + 1):
                yield (
                    cid, did, wid, did, wid,
                    _rdatetime(rng),
                    _rdec(rng, 10.00, 10.00, 2),
                    _rstr(rng, 12),
                )


def _tpcc_orders(rng: random.Random, w: int, d_per_w: int,
                 o_per_d: int) -> Iterator[tuple]:
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            for oid in range(1, o_per_d + 1):
                ol_cnt = rng.randint(5, 15)
                yield (
                    oid, did, wid,
                    rng.randint(1, o_per_d),         # o_c_id
                    _rdatetime(rng),
                    rng.randint(1, 10) if oid > o_per_d - 900 else None,  # carrier
                    ol_cnt,
                    1,
                )


def _tpcc_new_order(w: int, d_per_w: int, o_per_d: int,
                    no_per_d: int) -> Iterator[tuple]:
    first_no = o_per_d - no_per_d + 1
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            for oid in range(first_no, o_per_d + 1):
                yield (oid, did, wid)


def _tpcc_order_line(rng: random.Random, w: int, d_per_w: int,
                     o_per_d: int, no_per_d: int,
                     ol_per_order: int) -> Iterator[tuple]:
    first_no = o_per_d - no_per_d + 1
    for wid in range(1, w + 1):
        for did in range(1, d_per_w + 1):
            for oid in range(1, o_per_d + 1):
                n_lines = rng.randint(5, min(ol_per_order * 2 - 5, 15))
                is_new  = oid >= first_no
                for ln in range(1, n_lines + 1):
                    yield (
                        oid, did, wid, ln,
                        rng.randint(1, 100_000),     # ol_i_id
                        wid,                         # ol_supply_w_id
                        None if is_new else _rdatetime(rng),  # ol_delivery_d
                        5,                           # ol_quantity
                        0.0 if is_new else _rdec(rng, 0.01, 9999.99, 2),
                        _rstr(rng, 24),              # ol_dist_info
                    )


def _tpcc_stock(rng: random.Random, w: int, n_items: int) -> Iterator[tuple]:
    for wid in range(1, w + 1):
        for iid in range(1, n_items + 1):
            dist_cols = tuple(_rstr(rng, 24) for _ in range(10))
            yield (
                iid, wid,
                rng.randint(10, 100),                # s_quantity
                *dist_cols,
                0, 0, 0,                             # ytd, order_cnt, remote_cnt
                _rstr(rng, 26),                      # s_data
            )


def tpcc_rows(table: str, sf: int = 1, seed: int = 42) -> Iterator[tuple]:
    """
    Yield rows for the given TPC-C table at scale factor sf (warehouses).

    Parameters
    ----------
    table   One of: item, warehouse, district, customer, history,
            orders, new_order, order_line, stock.
    sf      Number of warehouses (scale factor).  Default 1.
    seed    Random seed for reproducibility.  Default 42.
    """
    from benchmarks.tpc_schemas import (
        DISTRICTS_PER_WAREHOUSE, CUSTOMERS_PER_DISTRICT,
        ORDERS_PER_DISTRICT, NEW_ORDERS_PER_DISTRICT,
        OL_PER_ORDER, ITEMS,
    )
    rng = random.Random(seed)
    w   = sf

    if table == "item":
        return _tpcc_item(rng, ITEMS)
    if table == "warehouse":
        return _tpcc_warehouse(rng, w)
    if table == "district":
        return _tpcc_district(rng, w, DISTRICTS_PER_WAREHOUSE)
    if table == "customer":
        return _tpcc_customer(rng, w, DISTRICTS_PER_WAREHOUSE, CUSTOMERS_PER_DISTRICT)
    if table == "history":
        return _tpcc_history(rng, w, DISTRICTS_PER_WAREHOUSE, CUSTOMERS_PER_DISTRICT)
    if table == "orders":
        return _tpcc_orders(rng, w, DISTRICTS_PER_WAREHOUSE, ORDERS_PER_DISTRICT)
    if table == "new_order":
        return _tpcc_new_order(w, DISTRICTS_PER_WAREHOUSE, ORDERS_PER_DISTRICT,
                               NEW_ORDERS_PER_DISTRICT)
    if table == "order_line":
        return _tpcc_order_line(rng, w, DISTRICTS_PER_WAREHOUSE, ORDERS_PER_DISTRICT,
                                NEW_ORDERS_PER_DISTRICT, OL_PER_ORDER)
    if table == "stock":
        return _tpcc_stock(rng, w, ITEMS)
    raise ValueError(f"Unknown TPC-C table: {table!r}")


# ---------------------------------------------------------------------------
# TPC-H generators
# ---------------------------------------------------------------------------

_REGION_NAMES  = ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"]
_NATION_NAMES  = [
    "ALGERIA", "ARGENTINA", "BRAZIL", "CANADA", "EGYPT",
    "ETHIOPIA", "FRANCE", "GERMANY", "INDIA", "INDONESIA",
    "IRAN", "IRAQ", "JAPAN", "JORDAN", "KENYA",
    "MOROCCO", "MOZAMBIQUE", "PERU", "CHINA", "ROMANIA",
    "SAUDI ARABIA", "VIETNAM", "RUSSIA", "UNITED KINGDOM", "UNITED STATES",
]
_NATION_REGIONS = [0,1,1,1,4,0,3,3,2,2,4,4,2,4,0,0,0,1,2,3,4,2,3,3,1]

_MKTSEGMENTS   = ["AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY"]
_PRIORITIES    = ["1-URGENT", "2-HIGH", "3-MEDIUM", "4-NOT SPECIFIED", "5-LOW"]
_SHIPMODES     = ["AIR", "FOB", "MAIL", "RAIL", "REG AIR", "SHIP", "TRUCK"]
_SHIPINSTRUCT  = ["COLLECT COD", "DELIVER IN PERSON", "NONE", "TAKE BACK RETURN"]
_CONTAINERS    = ["SM BOX", "SM CASE", "SM PACK", "SM PKG",
                  "MED BAG", "MED BOX", "MED PACK", "MED PKG",
                  "LG BOX", "LG CASE", "LG PACK", "LG PKG",
                  "WRAP BOX", "WRAP CASE", "WRAP PACK", "WRAP PKG"]
_SHIP_DATE_START = date(1992, 1, 1)
_SHIP_DATE_END   = date(1998, 12, 31)


def _tpch_region() -> Iterator[tuple]:
    for i, name in enumerate(_REGION_NAMES):
        yield (i, name.ljust(25), f"region comment {i}")


def _tpch_nation() -> Iterator[tuple]:
    for i, name in enumerate(_NATION_NAMES):
        yield (i, _NATION_REGIONS[i], name.ljust(25), f"nation comment {i}")


def _tpch_supplier(rng: random.Random, n: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        yield (
            i,
            f"Supplier#{i:>09}",
            _rstr(rng, 25),
            i % 25,                                  # s_nationkey
            _rstr(rng, 15, string.digits + "-"),     # s_phone
            _rdec(rng, -999.99, 9999.99, 2),
            _rstr(rng, 60),
        )


def _tpch_customer(rng: random.Random, n: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        yield (
            i,
            f"Customer#{i:>09}",
            _rstr(rng, 25),
            i % 25,                                  # c_nationkey
            _rstr(rng, 15, string.digits + "-"),
            _rdec(rng, -999.99, 9999.99, 2),
            rng.choice(_MKTSEGMENTS).ljust(10),
            _rstr(rng, 70),
        )


def _tpch_part(rng: random.Random, n: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        yield (
            i,
            _rstr(rng, 30),
            f"Manufacturer#{rng.randint(1,5)}".ljust(25),
            f"Brand#{rng.randint(1,5)}{rng.randint(1,5)}".ljust(10),
            _rstr(rng, 15),
            rng.randint(1, 50),
            rng.choice(_CONTAINERS).ljust(10),
            _rdec(rng, 900.00, 2100.00, 2),
            _rstr(rng, 15),
        )


def _tpch_partsupp(rng: random.Random, n_parts: int, n_supp: int,
                   mult: int) -> Iterator[tuple]:
    for pk in range(1, n_parts + 1):
        for s in range(mult):
            sk = (pk * mult + s) % n_supp + 1
            yield (
                pk, sk,
                rng.randint(1, 9999),
                _rdec(rng, 1.00, 1000.00, 2),
                _rstr(rng, 100),
            )


def _tpch_orders(rng: random.Random, n: int, n_cust: int) -> Iterator[tuple]:
    for i in range(1, n + 1):
        yield (
            i,
            rng.randint(1, n_cust),
            rng.choice(["F", "O", "P"]),
            _rdec(rng, 1000.00, 500000.00, 2),
            _rdate(rng, date(1992, 1, 1), date(1998, 8, 2)),
            rng.choice(_PRIORITIES).ljust(15),
            f"Clerk#{rng.randint(1, 1000):>09}",
            0,
            _rstr(rng, 45),
        )


def _tpch_lineitem(rng: random.Random, n_orders: int, n_parts: int,
                   n_supp: int) -> Iterator[tuple]:
    for ok in range(1, n_orders + 1):
        n_lines = rng.randint(1, 7)
        for ln in range(1, n_lines + 1):
            pk   = rng.randint(1, n_parts)
            sk   = (pk + rng.randint(0, 3)) % n_supp + 1
            ship = _rdate(rng, _SHIP_DATE_START, _SHIP_DATE_END)
            commit  = ship + timedelta(days=rng.randint(30, 90))
            receipt = ship + timedelta(days=rng.randint(1, 30))
            qty  = _rdec(rng, 1, 50, 2)
            price = _rdec(rng, 1.00, 100000.00, 2)
            disc  = _rdec(rng, 0.00, 0.10, 2)
            tax   = _rdec(rng, 0.00, 0.08, 2)
            yield (
                ok, pk, sk, ln,
                qty, price, disc, tax,
                rng.choice(["A", "N", "R"]),         # l_returnflag
                rng.choice(["F", "O"]),              # l_linestatus
                ship, commit, receipt,
                rng.choice(_SHIPINSTRUCT).ljust(25),
                rng.choice(_SHIPMODES).ljust(10),
                _rstr(rng, 28),
            )


def tpch_rows(table: str, sf: int = 1, seed: int = 42) -> Iterator[tuple]:
    """
    Yield rows for the given TPC-H table at scale factor sf.

    Parameters
    ----------
    table   One of: region, nation, supplier, customer, part,
            partsupp, orders, lineitem.
    sf      Scale factor (≈ GB of data).  Default 1.
    seed    Random seed for reproducibility.  Default 42.
    """
    from benchmarks.tpc_schemas import (
        TPCH_SUPPLIERS_PER_SF, TPCH_CUSTOMERS_PER_SF, TPCH_PARTS_PER_SF,
        TPCH_ORDERS_PER_SF, TPCH_PARTSUPP_MULT,
    )
    rng     = random.Random(seed)
    n_supp  = sf * TPCH_SUPPLIERS_PER_SF
    n_cust  = sf * TPCH_CUSTOMERS_PER_SF
    n_parts = sf * TPCH_PARTS_PER_SF
    n_ord   = sf * TPCH_ORDERS_PER_SF

    if table == "region":
        return _tpch_region()
    if table == "nation":
        return _tpch_nation()
    if table == "supplier":
        return _tpch_supplier(rng, n_supp)
    if table == "customer":
        return _tpch_customer(rng, n_cust)
    if table == "part":
        return _tpch_part(rng, n_parts)
    if table == "partsupp":
        return _tpch_partsupp(rng, n_parts, n_supp, TPCH_PARTSUPP_MULT)
    if table == "orders":
        return _tpch_orders(rng, n_ord, n_cust)
    if table == "lineitem":
        return _tpch_lineitem(rng, n_ord, n_parts, n_supp)
    raise ValueError(f"Unknown TPC-H table: {table!r}")
