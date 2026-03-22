"""
TPC-C and TPC-H schema definitions for the statschema load benchmark.

DDL is written in generic MySQL dialect and can be transpiled to any target
dialect via statschema's emit_ddl().  For benchmark purposes the raw DDL is
also available as a string so you can execute it directly.

Row count formulas follow the official TPC specifications:
  TPC-C: https://tpc.org/tpcc/
  TPC-H: https://tpc.org/tpch/

Both are parameterised by a scale factor (SF):
  - TPC-C: SF = number of warehouses (SF=1 → 1 warehouse)
  - TPC-H: SF = target database size in GB   (SF=1 → ~1 GB)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# TPC-C
# ---------------------------------------------------------------------------

TPCC_DDL = """
CREATE TABLE item (
    i_id     INT           NOT NULL,
    i_im_id  INT           NOT NULL,
    i_name   VARCHAR(24)   NOT NULL,
    i_price  DECIMAL(5,2)  NOT NULL,
    i_data   VARCHAR(50)   NOT NULL,
    PRIMARY KEY (i_id)
);

CREATE TABLE warehouse (
    w_id       INT            NOT NULL,
    w_name     VARCHAR(10)    NOT NULL,
    w_street_1 VARCHAR(20)    NOT NULL,
    w_street_2 VARCHAR(20)    NOT NULL,
    w_city     VARCHAR(20)    NOT NULL,
    w_state    CHAR(2)        NOT NULL,
    w_zip      CHAR(9)        NOT NULL,
    w_tax      DECIMAL(4,4)   NOT NULL,
    w_ytd      DECIMAL(12,2)  NOT NULL,
    PRIMARY KEY (w_id)
);

CREATE TABLE district (
    d_id         INT            NOT NULL,
    d_w_id       INT            NOT NULL,
    d_name       VARCHAR(10)    NOT NULL,
    d_street_1   VARCHAR(20)    NOT NULL,
    d_street_2   VARCHAR(20)    NOT NULL,
    d_city       VARCHAR(20)    NOT NULL,
    d_state      CHAR(2)        NOT NULL,
    d_zip        CHAR(9)        NOT NULL,
    d_tax        DECIMAL(4,4)   NOT NULL,
    d_ytd        DECIMAL(12,2)  NOT NULL,
    d_next_o_id  INT            NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

CREATE TABLE customer (
    c_id           INT            NOT NULL,
    c_d_id         INT            NOT NULL,
    c_w_id         INT            NOT NULL,
    c_first        VARCHAR(16)    NOT NULL,
    c_middle       CHAR(2)        NOT NULL,
    c_last         VARCHAR(16)    NOT NULL,
    c_street_1     VARCHAR(20)    NOT NULL,
    c_street_2     VARCHAR(20)    NOT NULL,
    c_city         VARCHAR(20)    NOT NULL,
    c_state        CHAR(2)        NOT NULL,
    c_zip          CHAR(9)        NOT NULL,
    c_phone        CHAR(16)       NOT NULL,
    c_since        DATETIME       NOT NULL,
    c_credit       CHAR(2)        NOT NULL,
    c_credit_lim   DECIMAL(12,2)  NOT NULL,
    c_discount     DECIMAL(4,4)   NOT NULL,
    c_balance      DECIMAL(12,2)  NOT NULL,
    c_ytd_payment  DECIMAL(12,2)  NOT NULL,
    c_payment_cnt  INT            NOT NULL,
    c_delivery_cnt INT            NOT NULL,
    c_data         VARCHAR(500)   NOT NULL,
    PRIMARY KEY (c_w_id, c_d_id, c_id)
);

CREATE TABLE history (
    h_c_id   INT            NOT NULL,
    h_c_d_id INT            NOT NULL,
    h_c_w_id INT            NOT NULL,
    h_d_id   INT            NOT NULL,
    h_w_id   INT            NOT NULL,
    h_date   DATETIME       NOT NULL,
    h_amount DECIMAL(6,2)   NOT NULL,
    h_data   VARCHAR(24)    NOT NULL
);

CREATE TABLE orders (
    o_id         INT       NOT NULL,
    o_d_id       INT       NOT NULL,
    o_w_id       INT       NOT NULL,
    o_c_id       INT       NOT NULL,
    o_entry_d    DATETIME  NOT NULL,
    o_carrier_id INT,
    o_ol_cnt     INT       NOT NULL,
    o_all_local  INT       NOT NULL,
    PRIMARY KEY (o_w_id, o_d_id, o_id)
);

CREATE TABLE new_order (
    no_o_id INT NOT NULL,
    no_d_id INT NOT NULL,
    no_w_id INT NOT NULL,
    PRIMARY KEY (no_w_id, no_d_id, no_o_id)
);

CREATE TABLE order_line (
    ol_o_id        INT            NOT NULL,
    ol_d_id        INT            NOT NULL,
    ol_w_id        INT            NOT NULL,
    ol_number      INT            NOT NULL,
    ol_i_id        INT            NOT NULL,
    ol_supply_w_id INT            NOT NULL,
    ol_delivery_d  DATETIME,
    ol_quantity    INT            NOT NULL,
    ol_amount      DECIMAL(6,2)   NOT NULL,
    ol_dist_info   CHAR(24)       NOT NULL,
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
);

CREATE TABLE stock (
    s_i_id       INT            NOT NULL,
    s_w_id       INT            NOT NULL,
    s_quantity   INT            NOT NULL,
    s_dist_01    CHAR(24)       NOT NULL,
    s_dist_02    CHAR(24)       NOT NULL,
    s_dist_03    CHAR(24)       NOT NULL,
    s_dist_04    CHAR(24)       NOT NULL,
    s_dist_05    CHAR(24)       NOT NULL,
    s_dist_06    CHAR(24)       NOT NULL,
    s_dist_07    CHAR(24)       NOT NULL,
    s_dist_08    CHAR(24)       NOT NULL,
    s_dist_09    CHAR(24)       NOT NULL,
    s_dist_10    CHAR(24)       NOT NULL,
    s_ytd        INT            NOT NULL,
    s_order_cnt  INT            NOT NULL,
    s_remote_cnt INT            NOT NULL,
    s_data       VARCHAR(50)    NOT NULL,
    PRIMARY KEY (s_w_id, s_i_id)
);
"""

# Columns in insert order for each TPC-C table
TPCC_COLUMNS: dict[str, list[str]] = {
    "item": ["i_id", "i_im_id", "i_name", "i_price", "i_data"],
    "warehouse": ["w_id", "w_name", "w_street_1", "w_street_2", "w_city",
                  "w_state", "w_zip", "w_tax", "w_ytd"],
    "district": ["d_id", "d_w_id", "d_name", "d_street_1", "d_street_2",
                 "d_city", "d_state", "d_zip", "d_tax", "d_ytd", "d_next_o_id"],
    "customer": ["c_id", "c_d_id", "c_w_id", "c_first", "c_middle", "c_last",
                 "c_street_1", "c_street_2", "c_city", "c_state", "c_zip",
                 "c_phone", "c_since", "c_credit", "c_credit_lim", "c_discount",
                 "c_balance", "c_ytd_payment", "c_payment_cnt", "c_delivery_cnt",
                 "c_data"],
    "history": ["h_c_id", "h_c_d_id", "h_c_w_id", "h_d_id", "h_w_id",
                "h_date", "h_amount", "h_data"],
    "orders": ["o_id", "o_d_id", "o_w_id", "o_c_id", "o_entry_d",
               "o_carrier_id", "o_ol_cnt", "o_all_local"],
    "new_order": ["no_o_id", "no_d_id", "no_w_id"],
    "order_line": ["ol_o_id", "ol_d_id", "ol_w_id", "ol_number", "ol_i_id",
                   "ol_supply_w_id", "ol_delivery_d", "ol_quantity", "ol_amount",
                   "ol_dist_info"],
    "stock": ["s_i_id", "s_w_id", "s_quantity",
              "s_dist_01", "s_dist_02", "s_dist_03", "s_dist_04", "s_dist_05",
              "s_dist_06", "s_dist_07", "s_dist_08", "s_dist_09", "s_dist_10",
              "s_ytd", "s_order_cnt", "s_remote_cnt", "s_data"],
}

# Load order respects FK dependencies
TPCC_LOAD_ORDER = [
    "item", "warehouse", "district", "customer", "history",
    "orders", "new_order", "order_line", "stock",
]

# Approximate TPC-C row counts at scale factor SF (= number of warehouses)
# Per TPC-C spec §1.2: each warehouse has 10 districts × 3000 customers.
DISTRICTS_PER_WAREHOUSE = 10
CUSTOMERS_PER_DISTRICT  = 3000
ORDERS_PER_DISTRICT     = 3000
NEW_ORDERS_PER_DISTRICT = 900   # last 30% of orders are new orders
OL_PER_ORDER            = 10    # average order-lines per order (spec: 5–15)
ITEMS                   = 100_000


def tpcc_row_counts(sf: int) -> dict[str, int]:
    """Return exact row counts for TPC-C at the given scale factor (warehouses)."""
    w = sf
    d = w * DISTRICTS_PER_WAREHOUSE
    c = d * CUSTOMERS_PER_DISTRICT
    o = d * ORDERS_PER_DISTRICT
    return {
        "item":       ITEMS,
        "warehouse":  w,
        "district":   d,
        "customer":   c,
        "history":    c,           # one history row per customer
        "orders":     o,
        "new_order":  d * NEW_ORDERS_PER_DISTRICT,
        "order_line": o * OL_PER_ORDER,
        "stock":      w * ITEMS,
    }


# ---------------------------------------------------------------------------
# TPC-H
# ---------------------------------------------------------------------------

TPCH_DDL = """
CREATE TABLE region (
    r_regionkey INT          NOT NULL,
    r_name      CHAR(25)     NOT NULL,
    r_comment   VARCHAR(152) NOT NULL,
    PRIMARY KEY (r_regionkey)
);

CREATE TABLE nation (
    n_nationkey INT          NOT NULL,
    n_regionkey INT          NOT NULL,
    n_name      CHAR(25)     NOT NULL,
    n_comment   VARCHAR(152) NOT NULL,
    PRIMARY KEY (n_nationkey)
);

CREATE TABLE supplier (
    s_suppkey   INT            NOT NULL,
    s_name      CHAR(25)       NOT NULL,
    s_address   VARCHAR(40)    NOT NULL,
    s_nationkey INT            NOT NULL,
    s_phone     CHAR(15)       NOT NULL,
    s_acctbal   DECIMAL(15,2)  NOT NULL,
    s_comment   VARCHAR(101)   NOT NULL,
    PRIMARY KEY (s_suppkey)
);

CREATE TABLE customer (
    c_custkey    INT            NOT NULL,
    c_name       VARCHAR(25)    NOT NULL,
    c_address    VARCHAR(40)    NOT NULL,
    c_nationkey  INT            NOT NULL,
    c_phone      CHAR(15)       NOT NULL,
    c_acctbal    DECIMAL(15,2)  NOT NULL,
    c_mktsegment CHAR(10)       NOT NULL,
    c_comment    VARCHAR(117)   NOT NULL,
    PRIMARY KEY (c_custkey)
);

CREATE TABLE part (
    p_partkey     INT            NOT NULL,
    p_name        VARCHAR(55)    NOT NULL,
    p_mfgr        CHAR(25)       NOT NULL,
    p_brand       CHAR(10)       NOT NULL,
    p_type        VARCHAR(25)    NOT NULL,
    p_size        INT            NOT NULL,
    p_container   CHAR(10)       NOT NULL,
    p_retailprice DECIMAL(15,2)  NOT NULL,
    p_comment     VARCHAR(23)    NOT NULL,
    PRIMARY KEY (p_partkey)
);

CREATE TABLE partsupp (
    ps_partkey    INT            NOT NULL,
    ps_suppkey    INT            NOT NULL,
    ps_availqty   INT            NOT NULL,
    ps_supplycost DECIMAL(15,2)  NOT NULL,
    ps_comment    VARCHAR(199)   NOT NULL,
    PRIMARY KEY (ps_partkey, ps_suppkey)
);

CREATE TABLE orders (
    o_orderkey      INT            NOT NULL,
    o_custkey       INT            NOT NULL,
    o_orderstatus   CHAR(1)        NOT NULL,
    o_totalprice    DECIMAL(15,2)  NOT NULL,
    o_orderdate     DATE           NOT NULL,
    o_orderpriority CHAR(15)       NOT NULL,
    o_clerk         CHAR(15)       NOT NULL,
    o_shippriority  INT            NOT NULL,
    o_comment       VARCHAR(79)    NOT NULL,
    PRIMARY KEY (o_orderkey)
);

CREATE TABLE lineitem (
    l_orderkey      INT            NOT NULL,
    l_partkey       INT            NOT NULL,
    l_suppkey       INT            NOT NULL,
    l_linenumber    INT            NOT NULL,
    l_quantity      DECIMAL(15,2)  NOT NULL,
    l_extendedprice DECIMAL(15,2)  NOT NULL,
    l_discount      DECIMAL(15,2)  NOT NULL,
    l_tax           DECIMAL(15,2)  NOT NULL,
    l_returnflag    CHAR(1)        NOT NULL,
    l_linestatus    CHAR(1)        NOT NULL,
    l_shipdate      DATE           NOT NULL,
    l_commitdate    DATE           NOT NULL,
    l_receiptdate   DATE           NOT NULL,
    l_shipinstruct  CHAR(25)       NOT NULL,
    l_shipmode      CHAR(10)       NOT NULL,
    l_comment       VARCHAR(44)    NOT NULL,
    PRIMARY KEY (l_orderkey, l_linenumber)
);
"""

TPCH_COLUMNS: dict[str, list[str]] = {
    "region":   ["r_regionkey", "r_name", "r_comment"],
    "nation":   ["n_nationkey", "n_regionkey", "n_name", "n_comment"],
    "supplier": ["s_suppkey", "s_name", "s_address", "s_nationkey", "s_phone",
                 "s_acctbal", "s_comment"],
    "customer": ["c_custkey", "c_name", "c_address", "c_nationkey", "c_phone",
                 "c_acctbal", "c_mktsegment", "c_comment"],
    "part":     ["p_partkey", "p_name", "p_mfgr", "p_brand", "p_type", "p_size",
                 "p_container", "p_retailprice", "p_comment"],
    "partsupp": ["ps_partkey", "ps_suppkey", "ps_availqty", "ps_supplycost",
                 "ps_comment"],
    "orders":   ["o_orderkey", "o_custkey", "o_orderstatus", "o_totalprice",
                 "o_orderdate", "o_orderpriority", "o_clerk", "o_shippriority",
                 "o_comment"],
    "lineitem": ["l_orderkey", "l_partkey", "l_suppkey", "l_linenumber",
                 "l_quantity", "l_extendedprice", "l_discount", "l_tax",
                 "l_returnflag", "l_linestatus", "l_shipdate", "l_commitdate",
                 "l_receiptdate", "l_shipinstruct", "l_shipmode", "l_comment"],
}

TPCH_LOAD_ORDER = [
    "region", "nation", "supplier", "customer", "part",
    "partsupp", "orders", "lineitem",
]

# TPC-H row counts at scale factor SF (≈ SF GB of data)
TPCH_SUPPLIERS_PER_SF = 10_000
TPCH_CUSTOMERS_PER_SF = 150_000
TPCH_PARTS_PER_SF     = 200_000
TPCH_ORDERS_PER_SF    = 1_500_000
TPCH_LINEITEMS_PER_SF = 6_000_000   # spec average; exact = 6,001,215 at SF=1
TPCH_PARTSUPP_MULT    = 4           # 4 suppliers per part


def tpch_row_counts(sf: int) -> dict[str, int]:
    """Return approximate row counts for TPC-H at the given scale factor."""
    return {
        "region":   5,
        "nation":   25,
        "supplier": sf * TPCH_SUPPLIERS_PER_SF,
        "customer": sf * TPCH_CUSTOMERS_PER_SF,
        "part":     sf * TPCH_PARTS_PER_SF,
        "partsupp": sf * TPCH_PARTS_PER_SF * TPCH_PARTSUPP_MULT,
        "orders":   sf * TPCH_ORDERS_PER_SF,
        "lineitem": sf * TPCH_LINEITEMS_PER_SF,
    }
