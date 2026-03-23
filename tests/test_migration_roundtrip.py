"""
Real-world migration round-trip tests.

Each test represents a migration story that a DBA would encounter:

  Northwind     SQL Server IDENTITY + NVARCHAR + ALTER TABLE FKs
                → represents: ERP, order-management systems (SAP, Dynamics)

  Sakila        MySQL AUTO_INCREMENT + composite PKs + inline FKs
                → represents: content-management / media library apps

  Django Auth   PostgreSQL SERIAL + inline REFERENCES + self-ref FK
                → represents: any Django web application (most popular Python web framework)

  WordPress     Mixed: InnoDB AUTO_INCREMENT, bigint PKs, foreign_key=0 style
                → represents: PHP CMS migrations (WordPress, Drupal, Joomla)

  Chinook       PostgreSQL INT PK + ALTER TABLE FKs (open-source music store)
                → represents: analytics / BI migrations across database platforms
                   (already smoke-tested interactively; included here for CI coverage)

For each schema the test:
  1. Parses the DDL and asserts the expected number of FK constraints are detected.
  2. Enriches the canonical model with row_count_per_sf and generation: rules.
  3. Generates data at SF=0.1 and loads into an in-memory SQLite database.
  4. Runs 5–10 representative queries (drawn from the schema's own query set) and
     asserts every query returns at least one non-zero result row.
"""

from __future__ import annotations

import sqlite3
import tempfile
import os
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_into_sqlite(
    tables_ordered,
    counts: dict[str, int],
    sqlite_schemas: dict[str, str],
) -> sqlite3.Connection:
    """Create an in-memory SQLite DB, create tables, and load generated rows."""
    from src.statschema.row_generator import generate_rows

    conn = sqlite3.connect(":memory:")
    for tbl_name, ddl in sqlite_schemas.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {tbl_name} ({ddl})")

    for tbl in tables_ordered:
        n = counts.get(tbl.name, 0)
        if n == 0:
            continue
        rows = list(generate_rows(tbl, n, parent_row_counts=counts))
        if not rows:
            continue
        cols = list(rows[0].keys())
        ph   = ",".join("?" * len(cols))
        conn.executemany(
            f"INSERT OR IGNORE INTO {tbl.name} ({','.join(cols)}) VALUES ({ph})",
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()
    return conn


def _assert_queries(conn: sqlite3.Connection, queries: list[tuple[str, str]]) -> None:
    """Run each (label, SQL) pair and assert it returns at least one non-zero row."""
    failures = []
    for label, sql in queries:
        try:
            rows = conn.execute(sql).fetchall()
        except Exception as e:
            failures.append(f"  {label}: ERROR — {e}")
            continue
        if not rows or (len(rows) == 1 and rows[0][0] in (0, None, 0.0, "")):
            failures.append(f"  {label}: zero / empty result — {rows}")
    if failures:
        pytest.fail("Query failures:\n" + "\n".join(failures))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Northwind  (SQL Server DDL)
#    Migration story: Dynamics 365 / SAP → PostgreSQL / CockroachDB
# ─────────────────────────────────────────────────────────────────────────────

NORTHWIND_DDL = """
CREATE TABLE categories (
    category_id   INT          NOT NULL,
    category_name NVARCHAR(15) NOT NULL,
    description   NTEXT,
    CONSTRAINT pk_categories PRIMARY KEY (category_id)
);

CREATE TABLE suppliers (
    supplier_id   INT          NOT NULL,
    company_name  NVARCHAR(40) NOT NULL,
    contact_name  NVARCHAR(30),
    city          NVARCHAR(15),
    country       NVARCHAR(15),
    phone         NVARCHAR(24),
    CONSTRAINT pk_suppliers PRIMARY KEY (supplier_id)
);

CREATE TABLE shippers (
    shipper_id    INT          NOT NULL,
    company_name  NVARCHAR(40) NOT NULL,
    phone         NVARCHAR(24),
    CONSTRAINT pk_shippers PRIMARY KEY (shipper_id)
);

CREATE TABLE employees (
    employee_id   INT          NOT NULL,
    last_name     NVARCHAR(20) NOT NULL,
    first_name    NVARCHAR(10) NOT NULL,
    title         NVARCHAR(30),
    reports_to    INT,
    hire_date     DATETIME,
    city          NVARCHAR(15),
    country       NVARCHAR(15),
    CONSTRAINT pk_employees PRIMARY KEY (employee_id)
);

CREATE TABLE customers (
    customer_id   NCHAR(5)     NOT NULL,
    company_name  NVARCHAR(40) NOT NULL,
    contact_name  NVARCHAR(30),
    city          NVARCHAR(15),
    country       NVARCHAR(15),
    phone         NVARCHAR(24),
    CONSTRAINT pk_customers PRIMARY KEY (customer_id)
);

CREATE TABLE products (
    product_id        INT          NOT NULL,
    product_name      NVARCHAR(40) NOT NULL,
    supplier_id       INT,
    category_id       INT,
    unit_price        MONEY,
    units_in_stock    SMALLINT,
    units_on_order    SMALLINT,
    discontinued      BIT          NOT NULL DEFAULT 0,
    CONSTRAINT pk_products PRIMARY KEY (product_id)
);

CREATE TABLE orders (
    order_id      INT      NOT NULL,
    customer_id   NCHAR(5),
    employee_id   INT,
    order_date    DATETIME,
    required_date DATETIME,
    shipped_date  DATETIME,
    ship_via      INT,
    freight       MONEY,
    ship_country  NVARCHAR(15),
    CONSTRAINT pk_orders PRIMARY KEY (order_id)
);

CREATE TABLE order_details (
    order_id    INT           NOT NULL,
    product_id  INT           NOT NULL,
    unit_price  MONEY         NOT NULL,
    quantity    SMALLINT      NOT NULL DEFAULT 1,
    discount    REAL          NOT NULL DEFAULT 0,
    CONSTRAINT pk_order_details PRIMARY KEY (order_id, product_id)
);

ALTER TABLE products      ADD CONSTRAINT fk_products_categories  FOREIGN KEY (category_id)  REFERENCES categories(category_id);
ALTER TABLE products      ADD CONSTRAINT fk_products_suppliers   FOREIGN KEY (supplier_id)  REFERENCES suppliers(supplier_id);
ALTER TABLE employees     ADD CONSTRAINT fk_employees_reports_to FOREIGN KEY (reports_to)   REFERENCES employees(employee_id);
ALTER TABLE orders        ADD CONSTRAINT fk_orders_customers     FOREIGN KEY (customer_id)  REFERENCES customers(customer_id);
ALTER TABLE orders        ADD CONSTRAINT fk_orders_employees     FOREIGN KEY (employee_id)  REFERENCES employees(employee_id);
ALTER TABLE orders        ADD CONSTRAINT fk_orders_shippers      FOREIGN KEY (ship_via)     REFERENCES shippers(shipper_id);
ALTER TABLE order_details ADD CONSTRAINT fk_od_orders            FOREIGN KEY (order_id)     REFERENCES orders(order_id);
ALTER TABLE order_details ADD CONSTRAINT fk_od_products          FOREIGN KEY (product_id)   REFERENCES products(product_id);
"""

NORTHWIND_SQLITE = {
    "categories":    "category_id INTEGER PRIMARY KEY, category_name TEXT, description TEXT",
    "suppliers":     "supplier_id INTEGER PRIMARY KEY, company_name TEXT, contact_name TEXT, city TEXT, country TEXT, phone TEXT",
    "shippers":      "shipper_id INTEGER PRIMARY KEY, company_name TEXT, phone TEXT",
    "employees":     "employee_id INTEGER PRIMARY KEY, last_name TEXT, first_name TEXT, title TEXT, reports_to INTEGER, hire_date TEXT, city TEXT, country TEXT",
    "customers":     "customer_id TEXT PRIMARY KEY, company_name TEXT, contact_name TEXT, city TEXT, country TEXT, phone TEXT",
    "products":      "product_id INTEGER PRIMARY KEY, product_name TEXT, supplier_id INTEGER, category_id INTEGER, unit_price REAL, units_in_stock INTEGER, units_on_order INTEGER, discontinued INTEGER",
    "orders":        "order_id INTEGER PRIMARY KEY, customer_id TEXT, employee_id INTEGER, order_date TEXT, required_date TEXT, shipped_date TEXT, ship_via INTEGER, freight REAL, ship_country TEXT",
    "order_details": "order_id INTEGER, product_id INTEGER, unit_price REAL, quantity INTEGER, discount REAL, PRIMARY KEY(order_id, product_id)",
}

NORTHWIND_QUERIES = [
    ("Total orders",             "SELECT COUNT(*) FROM orders"),
    ("Revenue by country",       "SELECT ship_country, ROUND(SUM(od.unit_price*od.quantity),2) FROM orders o JOIN order_details od ON o.order_id=od.order_id GROUP BY ship_country ORDER BY 2 DESC LIMIT 5"),
    ("Products by category",     "SELECT c.category_name, COUNT(*) FROM products p JOIN categories c ON p.category_id=c.category_id GROUP BY c.category_name LIMIT 5"),
    ("Employee order count",     "SELECT e.last_name, COUNT(o.order_id) FROM orders o JOIN employees e ON o.employee_id=e.employee_id GROUP BY e.last_name ORDER BY 2 DESC LIMIT 5"),
    ("Top products by revenue",  "SELECT p.product_name, ROUND(SUM(od.unit_price*od.quantity),2) FROM order_details od JOIN products p ON od.product_id=p.product_id GROUP BY p.product_name ORDER BY 2 DESC LIMIT 5"),
    ("Supplier product count",   "SELECT s.company_name, COUNT(*) FROM products p JOIN suppliers s ON p.supplier_id=s.supplier_id GROUP BY s.company_name LIMIT 5"),
    ("Shipper usage",            "SELECT s.company_name, COUNT(*) FROM orders o JOIN shippers s ON o.ship_via=s.shipper_id GROUP BY s.company_name"),
]


def test_northwind_sqlserver_migration():
    """SQL Server Northwind: NVARCHAR, MONEY, SMALLINT, NCHAR(5) customer PK."""
    from src.statschema.ddl_parser import parse_ddl
    from src.statschema.model import GenerationRule
    from src.statschema.schema_io import resolve_row_counts
    from src.statschema.schema_io import resolve_load_order

    tables = parse_ddl(NORTHWIND_DDL, dialect="sqlserver")
    tbl_map = {t.name: t for t in tables}

    # Verify FK detection
    total_fks = sum(len(t.fk_constraints or []) for t in tables)
    assert total_fks == 8, f"Expected 8 FKs, got {total_fks}"

    # Sizing (approximate Northwind row counts)
    tbl_map["categories"].row_count  = 8
    tbl_map["suppliers"].row_count   = 29
    tbl_map["shippers"].row_count    = 3
    tbl_map["employees"].row_count   = 9
    for name, rpsf in [("customers",91), ("products",77), ("orders",830), ("order_details",2155)]:
        tbl_map[name].row_count_per_sf = float(rpsf)

    def sg(tbl, col, **kw):
        c = next(x for x in tbl_map[tbl].columns if x.name == col)
        c.generation = GenerationRule(**kw)

    sg("categories",   "category_name", values=["Beverages","Condiments","Confections","Dairy Products","Grains/Cereals","Meat/Poultry","Produce","Seafood"])
    sg("employees",    "title",         values=["Sales Representative","Sales Manager","Vice President Sales","Inside Sales Coordinator"])
    sg("employees",    "country",       values=["USA","UK"])
    sg("products",     "unit_price",    min_value=2.5, max_value=263.5)
    sg("products",     "units_in_stock",min_value=0, max_value=125)
    sg("order_details","unit_price",    min_value=2.5, max_value=263.5)
    sg("order_details","quantity",      min_value=1, max_value=130)
    sg("order_details","discount",      values=[0.0, 0.05, 0.1, 0.15, 0.2, 0.25])
    sg("orders",       "freight",       min_value=0.0, max_value=1007.64)
    sg("orders",       "ship_country",  values=["Germany","USA","Brazil","France","UK","Canada","Argentina","Switzerland","Australia"])
    # customers uses TEXT PK — keep sequential ints cast to TEXT
    for col in tbl_map["customers"].columns:
        if col.name == "customer_id":
            col.generation = GenerationRule(distribution="sequential", min_value=1)

    counts  = resolve_row_counts(tables, scale_factor=0.1)
    ordered = resolve_load_order(tables)
    conn    = _load_into_sqlite(ordered, counts, NORTHWIND_SQLITE)
    _assert_queries(conn, NORTHWIND_QUERIES)
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sakila  (MySQL DDL)
#    Migration story: MySQL 5.7 video-rental app → Aurora PostgreSQL
#    Tests: AUTO_INCREMENT, TINYINT UNSIGNED, composite FK (film_actor, film_category)
#    multi-level chain: payment → rental → inventory → film → language
# ─────────────────────────────────────────────────────────────────────────────

SAKILA_DDL = """
CREATE TABLE language (
    language_id   TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name          CHAR(20)         NOT NULL,
    last_update   TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (language_id)
) ENGINE=InnoDB;

CREATE TABLE category (
    category_id  TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name         VARCHAR(25)      NOT NULL,
    last_update  TIMESTAMP        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY  (category_id)
) ENGINE=InnoDB;

CREATE TABLE actor (
    actor_id    SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    first_name  VARCHAR(45)       NOT NULL,
    last_name   VARCHAR(45)       NOT NULL,
    last_update TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (actor_id)
) ENGINE=InnoDB;

CREATE TABLE film (
    film_id          SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    title            VARCHAR(128)      NOT NULL,
    description      TEXT,
    release_year     YEAR,
    language_id      TINYINT UNSIGNED  NOT NULL,
    rental_duration  TINYINT UNSIGNED  NOT NULL DEFAULT 3,
    rental_rate      DECIMAL(4,2)      NOT NULL DEFAULT 4.99,
    length           SMALLINT UNSIGNED,
    replacement_cost DECIMAL(5,2)      NOT NULL DEFAULT 19.99,
    rating           ENUM('G','PG','PG-13','R','NC-17') DEFAULT 'G',
    PRIMARY KEY (film_id),
    CONSTRAINT fk_film_language FOREIGN KEY (language_id) REFERENCES language (language_id)
) ENGINE=InnoDB;

CREATE TABLE film_actor (
    actor_id    SMALLINT UNSIGNED NOT NULL,
    film_id     SMALLINT UNSIGNED NOT NULL,
    last_update TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (actor_id, film_id),
    CONSTRAINT fk_film_actor_actor FOREIGN KEY (actor_id) REFERENCES actor (actor_id),
    CONSTRAINT fk_film_actor_film  FOREIGN KEY (film_id)  REFERENCES film  (film_id)
) ENGINE=InnoDB;

CREATE TABLE film_category (
    film_id     SMALLINT UNSIGNED NOT NULL,
    category_id TINYINT UNSIGNED  NOT NULL,
    last_update TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (film_id, category_id),
    CONSTRAINT fk_film_category_film     FOREIGN KEY (film_id)     REFERENCES film     (film_id),
    CONSTRAINT fk_film_category_category FOREIGN KEY (category_id) REFERENCES category (category_id)
) ENGINE=InnoDB;

CREATE TABLE country (
    country_id  SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    country     VARCHAR(50)       NOT NULL,
    last_update TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (country_id)
) ENGINE=InnoDB;

CREATE TABLE city (
    city_id     SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    city        VARCHAR(50)       NOT NULL,
    country_id  SMALLINT UNSIGNED NOT NULL,
    last_update TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (city_id),
    CONSTRAINT fk_city_country FOREIGN KEY (country_id) REFERENCES country (country_id)
) ENGINE=InnoDB;

CREATE TABLE address (
    address_id  SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    address     VARCHAR(50)       NOT NULL,
    district    VARCHAR(20)       NOT NULL,
    city_id     SMALLINT UNSIGNED NOT NULL,
    postal_code VARCHAR(10),
    phone       VARCHAR(20)       NOT NULL,
    last_update TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (address_id),
    CONSTRAINT fk_address_city FOREIGN KEY (city_id) REFERENCES city (city_id)
) ENGINE=InnoDB;

CREATE TABLE store (
    store_id         TINYINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    manager_staff_id TINYINT UNSIGNED  NOT NULL,
    address_id       SMALLINT UNSIGNED NOT NULL,
    last_update      TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (store_id)
) ENGINE=InnoDB;

CREATE TABLE staff (
    staff_id    TINYINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    first_name  VARCHAR(45)       NOT NULL,
    last_name   VARCHAR(45)       NOT NULL,
    address_id  SMALLINT UNSIGNED NOT NULL,
    email       VARCHAR(50),
    store_id    TINYINT UNSIGNED  NOT NULL,
    username    VARCHAR(16)       NOT NULL,
    password    VARCHAR(40),
    active      TINYINT(1)        NOT NULL DEFAULT 1,
    last_update TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (staff_id),
    CONSTRAINT fk_staff_address FOREIGN KEY (address_id) REFERENCES address (address_id),
    CONSTRAINT fk_staff_store   FOREIGN KEY (store_id)   REFERENCES store   (store_id)
) ENGINE=InnoDB;

CREATE TABLE customer (
    customer_id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    store_id    TINYINT UNSIGNED  NOT NULL,
    first_name  VARCHAR(45)       NOT NULL,
    last_name   VARCHAR(45)       NOT NULL,
    email       VARCHAR(50),
    address_id  SMALLINT UNSIGNED NOT NULL,
    active      TINYINT(1)        NOT NULL DEFAULT 1,
    create_date DATETIME          NOT NULL,
    last_update TIMESTAMP,
    PRIMARY KEY (customer_id),
    CONSTRAINT fk_customer_address FOREIGN KEY (address_id) REFERENCES address (address_id),
    CONSTRAINT fk_customer_store   FOREIGN KEY (store_id)   REFERENCES store   (store_id)
) ENGINE=InnoDB;

CREATE TABLE inventory (
    inventory_id MEDIUMINT UNSIGNED NOT NULL AUTO_INCREMENT,
    film_id      SMALLINT UNSIGNED  NOT NULL,
    store_id     TINYINT UNSIGNED   NOT NULL,
    last_update  TIMESTAMP          NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (inventory_id),
    CONSTRAINT fk_inventory_film  FOREIGN KEY (film_id)  REFERENCES film  (film_id),
    CONSTRAINT fk_inventory_store FOREIGN KEY (store_id) REFERENCES store (store_id)
) ENGINE=InnoDB;

CREATE TABLE rental (
    rental_id    INT               NOT NULL AUTO_INCREMENT,
    rental_date  DATETIME          NOT NULL,
    inventory_id MEDIUMINT UNSIGNED NOT NULL,
    customer_id  SMALLINT UNSIGNED  NOT NULL,
    return_date  DATETIME,
    staff_id     TINYINT UNSIGNED   NOT NULL,
    last_update  TIMESTAMP          NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (rental_id),
    CONSTRAINT fk_rental_inventory FOREIGN KEY (inventory_id) REFERENCES inventory (inventory_id),
    CONSTRAINT fk_rental_customer  FOREIGN KEY (customer_id)  REFERENCES customer  (customer_id),
    CONSTRAINT fk_rental_staff     FOREIGN KEY (staff_id)     REFERENCES staff     (staff_id)
) ENGINE=InnoDB;

CREATE TABLE payment (
    payment_id   SMALLINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    customer_id  SMALLINT UNSIGNED  NOT NULL,
    staff_id     TINYINT UNSIGNED   NOT NULL,
    rental_id    INT,
    amount       DECIMAL(5,2)       NOT NULL,
    payment_date DATETIME           NOT NULL,
    last_update  TIMESTAMP,
    PRIMARY KEY (payment_id),
    CONSTRAINT fk_payment_customer FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_payment_staff    FOREIGN KEY (staff_id)    REFERENCES staff    (staff_id),
    CONSTRAINT fk_payment_rental   FOREIGN KEY (rental_id)   REFERENCES rental   (rental_id)
) ENGINE=InnoDB;
"""

SAKILA_SQLITE = {
    "language":      "language_id INTEGER PRIMARY KEY, name TEXT, last_update TEXT",
    "category":      "category_id INTEGER PRIMARY KEY, name TEXT, last_update TEXT",
    "actor":         "actor_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, last_update TEXT",
    "country":       "country_id INTEGER PRIMARY KEY, country TEXT, last_update TEXT",
    "city":          "city_id INTEGER PRIMARY KEY, city TEXT, country_id INTEGER, last_update TEXT",
    "address":       "address_id INTEGER PRIMARY KEY, address TEXT, district TEXT, city_id INTEGER, postal_code TEXT, phone TEXT, last_update TEXT",
    "store":         "store_id INTEGER PRIMARY KEY, manager_staff_id INTEGER, address_id INTEGER, last_update TEXT",
    "film":          "film_id INTEGER PRIMARY KEY, title TEXT, description TEXT, release_year INTEGER, language_id INTEGER, rental_duration INTEGER, rental_rate REAL, length INTEGER, replacement_cost REAL, rating TEXT",
    "film_actor":    "actor_id INTEGER, film_id INTEGER, last_update TEXT, PRIMARY KEY(actor_id, film_id)",
    "film_category": "film_id INTEGER, category_id INTEGER, last_update TEXT, PRIMARY KEY(film_id, category_id)",
    "staff":         "staff_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, address_id INTEGER, email TEXT, store_id INTEGER, username TEXT, password TEXT, active INTEGER, last_update TEXT",
    "customer":      "customer_id INTEGER PRIMARY KEY, store_id INTEGER, first_name TEXT, last_name TEXT, email TEXT, address_id INTEGER, active INTEGER, create_date TEXT, last_update TEXT",
    "inventory":     "inventory_id INTEGER PRIMARY KEY, film_id INTEGER, store_id INTEGER, last_update TEXT",
    "rental":        "rental_id INTEGER PRIMARY KEY, rental_date TEXT, inventory_id INTEGER, customer_id INTEGER, return_date TEXT, staff_id INTEGER, last_update TEXT",
    "payment":       "payment_id INTEGER PRIMARY KEY, customer_id INTEGER, staff_id INTEGER, rental_id INTEGER, amount REAL, payment_date TEXT, last_update TEXT",
}

SAKILA_QUERIES = [
    ("Total payments",             "SELECT COUNT(*) FROM payment"),
    ("Total revenue",              "SELECT ROUND(SUM(amount),2) FROM payment"),
    ("Revenue by store",           "SELECT s.store_id, ROUND(SUM(p.amount),2) FROM payment p JOIN staff st ON p.staff_id=st.staff_id JOIN store s ON st.store_id=s.store_id GROUP BY s.store_id"),
    ("Top 5 customers by spend",   "SELECT c.first_name||' '||c.last_name, ROUND(SUM(p.amount),2) FROM payment p JOIN customer c ON p.customer_id=c.customer_id GROUP BY c.customer_id ORDER BY 2 DESC LIMIT 5"),
    ("Films per category",         "SELECT cat.name, COUNT(*) FROM film_category fc JOIN category cat ON fc.category_id=cat.category_id GROUP BY cat.name ORDER BY 2 DESC LIMIT 5"),
    ("Actors in most films",       "SELECT a.first_name||' '||a.last_name, COUNT(*) FROM film_actor fa JOIN actor a ON fa.actor_id=a.actor_id GROUP BY a.actor_id ORDER BY 2 DESC LIMIT 5"),
    ("Full chain: pay→rent→film",  "SELECT COUNT(*) FROM payment p JOIN rental r ON p.rental_id=r.rental_id JOIN inventory i ON r.inventory_id=i.inventory_id JOIN film f ON i.film_id=f.film_id"),
    ("Film rental rates",          "SELECT rating, ROUND(AVG(rental_rate),2) FROM film GROUP BY rating ORDER BY 2 DESC"),
    ("Country → city → rental",    "SELECT co.country, COUNT(r.rental_id) FROM rental r JOIN customer c ON r.customer_id=c.customer_id JOIN address a ON c.address_id=a.address_id JOIN city ci ON a.city_id=ci.city_id JOIN country co ON ci.country_id=co.country_id GROUP BY co.country ORDER BY 2 DESC LIMIT 5"),
]


def test_sakila_mysql_migration():
    """MySQL Sakila: AUTO_INCREMENT, TINYINT UNSIGNED, deep 5-table JOIN chain."""
    from src.statschema.ddl_parser import parse_ddl
    from src.statschema.model import GenerationRule
    from src.statschema.schema_io import resolve_row_counts
    from src.statschema.schema_io import resolve_load_order

    tables = parse_ddl(SAKILA_DDL, dialect="mysql")
    tbl_map = {t.name: t for t in tables}

    # Verify FK detection
    total_fks = sum(len(t.fk_constraints or []) for t in tables)
    assert total_fks >= 14, f"Expected ≥14 FKs, got {total_fks}"

    # Fixed/small lookup tables
    tbl_map["language"].row_count  = 6
    tbl_map["category"].row_count  = 16
    tbl_map["country"].row_count   = 109
    tbl_map["store"].row_count     = 2
    tbl_map["staff"].row_count     = 2

    # Scaling tables (real Sakila counts)
    for name, rpsf in [
        ("actor",200), ("city",600), ("address",603),
        ("film",1000), ("film_actor",5462), ("film_category",1000),
        ("customer",599), ("inventory",4581), ("rental",16044), ("payment",16049),
    ]:
        tbl_map[name].row_count_per_sf = float(rpsf)

    def sg(tbl, col, **kw):
        c = next(x for x in tbl_map[tbl].columns if x.name == col)
        c.generation = GenerationRule(**kw)

    sg("language",  "name",            values=["English","Italian","Japanese","Mandarin","French","German"])
    sg("category",  "name",            values=["Action","Animation","Children","Classics","Comedy","Documentary","Drama","Family","Foreign","Games","Horror","Music","New","Sci-Fi","Sports","Travel"])
    sg("country",   "country",         format_pattern="word")
    sg("city",      "city",            format_pattern="word")
    sg("film",      "title",           format_pattern="word")
    sg("film",      "rental_rate",     values=[0.99, 2.99, 4.99])
    sg("film",      "replacement_cost",values=[9.99, 14.99, 19.99, 24.99, 29.99])
    sg("film",      "rental_duration", min_value=3, max_value=7)
    sg("film",      "length",          min_value=46, max_value=185)
    sg("film",      "release_year",    min_value=2000, max_value=2006)
    sg("film",      "rating",          values=["G","PG","PG-13","R","NC-17"])
    sg("actor",     "first_name",      format_pattern="first_name")
    sg("actor",     "last_name",       format_pattern="last_name")
    sg("customer",  "first_name",      format_pattern="first_name")
    sg("customer",  "last_name",       format_pattern="last_name")
    sg("customer",  "email",           format_pattern="email")
    sg("staff",     "first_name",      format_pattern="first_name")
    sg("staff",     "last_name",       format_pattern="last_name")
    sg("payment",   "amount",          values=[0.99, 1.99, 2.99, 3.99, 4.99, 5.99, 6.99, 7.99, 8.99, 9.99, 10.99])

    counts  = resolve_row_counts(tables, scale_factor=0.1)
    ordered = resolve_load_order(tables)
    conn    = _load_into_sqlite(ordered, counts, SAKILA_SQLITE)
    _assert_queries(conn, SAKILA_QUERIES)
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Django Auth + content types
#    Migration story: Django 4.x default database schema → any target DB
#    Tests: SERIAL PKs, inline REFERENCES, self-referencing permission/group model
#    This is the schema every Django app starts from.
# ─────────────────────────────────────────────────────────────────────────────

DJANGO_AUTH_DDL = """
CREATE TABLE auth_user (
    id           SERIAL       PRIMARY KEY,
    password     VARCHAR(128) NOT NULL,
    last_login   TIMESTAMP,
    is_superuser BOOLEAN      NOT NULL DEFAULT false,
    username     VARCHAR(150) NOT NULL UNIQUE,
    first_name   VARCHAR(150) NOT NULL DEFAULT '',
    last_name    VARCHAR(150) NOT NULL DEFAULT '',
    email        VARCHAR(254) NOT NULL DEFAULT '',
    is_staff     BOOLEAN      NOT NULL DEFAULT false,
    is_active    BOOLEAN      NOT NULL DEFAULT true,
    date_joined  TIMESTAMP    NOT NULL
);

CREATE TABLE auth_group (
    id   SERIAL       PRIMARY KEY,
    name VARCHAR(150) NOT NULL UNIQUE
);

CREATE TABLE django_content_type (
    id        SERIAL       PRIMARY KEY,
    app_label VARCHAR(100) NOT NULL,
    model     VARCHAR(100) NOT NULL
);

CREATE TABLE auth_permission (
    id              SERIAL       PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    content_type_id INTEGER      NOT NULL REFERENCES django_content_type(id),
    codename        VARCHAR(100) NOT NULL
);

CREATE TABLE auth_group_permissions (
    id            SERIAL  PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES auth_group(id),
    permission_id INTEGER NOT NULL REFERENCES auth_permission(id)
);

CREATE TABLE auth_user_groups (
    id       SERIAL  PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES auth_user(id),
    group_id INTEGER NOT NULL REFERENCES auth_group(id)
);

CREATE TABLE auth_user_user_permissions (
    id            SERIAL  PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES auth_user(id),
    permission_id INTEGER NOT NULL REFERENCES auth_permission(id)
);

CREATE TABLE django_session (
    session_key  VARCHAR(40)  NOT NULL PRIMARY KEY,
    session_data TEXT         NOT NULL,
    expire_date  TIMESTAMP    NOT NULL
);

CREATE TABLE django_admin_log (
    id              SERIAL       PRIMARY KEY,
    action_time     TIMESTAMP    NOT NULL,
    object_id       TEXT,
    object_repr     VARCHAR(200) NOT NULL,
    action_flag     SMALLINT     NOT NULL,
    change_message  TEXT         NOT NULL DEFAULT '',
    content_type_id INTEGER      REFERENCES django_content_type(id),
    user_id         INTEGER      NOT NULL REFERENCES auth_user(id)
);
"""

DJANGO_SQLITE = {
    "auth_user":                   "id INTEGER PRIMARY KEY, password TEXT, last_login TEXT, is_superuser INTEGER, username TEXT, first_name TEXT, last_name TEXT, email TEXT, is_staff INTEGER, is_active INTEGER, date_joined TEXT",
    "auth_group":                  "id INTEGER PRIMARY KEY, name TEXT",
    "django_content_type":         "id INTEGER PRIMARY KEY, app_label TEXT, model TEXT",
    "auth_permission":             "id INTEGER PRIMARY KEY, name TEXT, content_type_id INTEGER, codename TEXT",
    "auth_group_permissions":      "id INTEGER PRIMARY KEY, group_id INTEGER, permission_id INTEGER",
    "auth_user_groups":            "id INTEGER PRIMARY KEY, user_id INTEGER, group_id INTEGER",
    "auth_user_user_permissions":  "id INTEGER PRIMARY KEY, user_id INTEGER, permission_id INTEGER",
    "django_session":              "session_key TEXT PRIMARY KEY, session_data TEXT, expire_date TEXT",
    "django_admin_log":            "id INTEGER PRIMARY KEY, action_time TEXT, object_id TEXT, object_repr TEXT, action_flag INTEGER, change_message TEXT, content_type_id INTEGER, user_id INTEGER",
}

DJANGO_QUERIES = [
    ("Total users",                   "SELECT COUNT(*) FROM auth_user"),
    ("Active users",                  "SELECT COUNT(*) FROM auth_user WHERE is_active=1"),
    ("Permissions per content type",  "SELECT ct.app_label, COUNT(p.id) FROM auth_permission p JOIN django_content_type ct ON p.content_type_id=ct.id GROUP BY ct.app_label ORDER BY 2 DESC LIMIT 5"),
    ("Users in each group",           "SELECT g.name, COUNT(ug.user_id) FROM auth_group g JOIN auth_user_groups ug ON g.id=ug.group_id GROUP BY g.name ORDER BY 2 DESC LIMIT 5"),
    ("Admin log entries",             "SELECT COUNT(*) FROM django_admin_log"),
    ("Admin log with user+type",      "SELECT COUNT(*) FROM django_admin_log al JOIN auth_user u ON al.user_id=u.id JOIN django_content_type ct ON al.content_type_id=ct.id"),
    ("Permissions per group",         "SELECT g.name, COUNT(gp.permission_id) FROM auth_group g JOIN auth_group_permissions gp ON g.id=gp.group_id GROUP BY g.name ORDER BY 2 DESC LIMIT 5"),
]


def test_django_auth_migration():
    """Django Auth: SERIAL PKs, inline REFERENCES, many-to-many membership tables."""
    from src.statschema.ddl_parser import parse_ddl
    from src.statschema.model import GenerationRule
    from src.statschema.schema_io import resolve_row_counts
    from src.statschema.schema_io import resolve_load_order

    tables = parse_ddl(DJANGO_AUTH_DDL, dialect="postgres")
    tbl_map = {t.name: t for t in tables}

    # Verify inline REFERENCES were detected
    total_fks = sum(len(t.fk_constraints or []) for t in tables)
    assert total_fks >= 7, f"Expected ≥7 FKs, got {total_fks}"

    # Sizing — Django auth tables are small for most apps
    tbl_map["django_session"].row_count_per_sf = 5000.0
    for name, rpsf in [
        ("auth_user",500), ("auth_group",20), ("django_content_type",30),
        ("auth_permission",90), ("auth_group_permissions",150),
        ("auth_user_groups",400), ("auth_user_user_permissions",200),
        ("django_admin_log",1000),
    ]:
        tbl_map[name].row_count_per_sf = float(rpsf)

    def sg(tbl, col, **kw):
        c = next(x for x in tbl_map[tbl].columns if x.name == col)
        c.generation = GenerationRule(**kw)

    sg("auth_user", "username",    format_pattern="word")
    sg("auth_user", "email",       format_pattern="email")
    sg("auth_user", "first_name",  format_pattern="first_name")
    sg("auth_user", "last_name",   format_pattern="last_name")
    sg("auth_user", "is_active",   values=[1])
    sg("auth_user", "is_staff",    values=[0, 1])
    sg("auth_user", "is_superuser",values=[0])
    sg("auth_group","name",        format_pattern="word")
    sg("django_content_type", "app_label", values=["auth","contenttypes","admin","sessions","myapp"])
    sg("django_content_type", "model",     values=["user","group","permission","logentry","session","product","order"])
    sg("auth_permission","codename",       format_pattern="word")
    sg("django_admin_log","action_flag",   values=[1, 2, 3])

    counts  = resolve_row_counts(tables, scale_factor=0.1)
    ordered = resolve_load_order(tables)
    conn    = _load_into_sqlite(ordered, counts, DJANGO_SQLITE)
    _assert_queries(conn, DJANGO_QUERIES)
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. WordPress (MySQL InnoDB)
#    Migration story: WordPress MySQL 8.0 → AWS Aurora PostgreSQL
#    Tests: bigint PKs, wp_ prefix tables, longtext columns, mixed FK styles
#    (WordPress uses no FK constraints in its DDL by design; FKs are logical)
#    We add them manually to demonstrate the "enrich" step a DBA does once.
# ─────────────────────────────────────────────────────────────────────────────

WORDPRESS_DDL = """
CREATE TABLE wp_users (
    ID                  bigint(20)    UNSIGNED NOT NULL AUTO_INCREMENT,
    user_login          varchar(60)   NOT NULL DEFAULT '',
    user_pass           varchar(255)  NOT NULL DEFAULT '',
    user_nicename       varchar(50)   NOT NULL DEFAULT '',
    user_email          varchar(100)  NOT NULL DEFAULT '',
    user_registered     datetime      NOT NULL DEFAULT '0000-00-00 00:00:00',
    user_status         int(11)       NOT NULL DEFAULT 0,
    display_name        varchar(250)  NOT NULL DEFAULT '',
    PRIMARY KEY (ID)
) ENGINE=InnoDB;

CREATE TABLE wp_usermeta (
    umeta_id    bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id     bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    meta_key    varchar(255),
    meta_value  longtext,
    PRIMARY KEY (umeta_id)
) ENGINE=InnoDB;

CREATE TABLE wp_posts (
    ID                    bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    post_author           bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    post_date             datetime            NOT NULL DEFAULT '0000-00-00 00:00:00',
    post_content          longtext            NOT NULL,
    post_title            text                NOT NULL,
    post_status           varchar(20)         NOT NULL DEFAULT 'publish',
    post_type             varchar(20)         NOT NULL DEFAULT 'post',
    post_parent           bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    comment_count         bigint(20)          NOT NULL DEFAULT 0,
    PRIMARY KEY (ID)
) ENGINE=InnoDB;

CREATE TABLE wp_postmeta (
    meta_id     bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    post_id     bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    meta_key    varchar(255),
    meta_value  longtext,
    PRIMARY KEY (meta_id)
) ENGINE=InnoDB;

CREATE TABLE wp_terms (
    term_id    bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    name       varchar(200)        NOT NULL DEFAULT '',
    slug       varchar(200)        NOT NULL DEFAULT '',
    term_group bigint(10)          NOT NULL DEFAULT 0,
    PRIMARY KEY (term_id)
) ENGINE=InnoDB;

CREATE TABLE wp_term_taxonomy (
    term_taxonomy_id bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    term_id          bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    taxonomy         varchar(32)         NOT NULL DEFAULT '',
    description      longtext            NOT NULL,
    parent           bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    count            bigint(20)          NOT NULL DEFAULT 0,
    PRIMARY KEY (term_taxonomy_id)
) ENGINE=InnoDB;

CREATE TABLE wp_term_relationships (
    object_id        bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    term_taxonomy_id bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    term_order       int(11)             NOT NULL DEFAULT 0,
    PRIMARY KEY (object_id, term_taxonomy_id)
) ENGINE=InnoDB;

CREATE TABLE wp_comments (
    comment_ID           bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    comment_post_ID      bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    comment_author       tinytext            NOT NULL,
    comment_author_email varchar(100)        NOT NULL DEFAULT '',
    comment_date         datetime            NOT NULL DEFAULT '0000-00-00 00:00:00',
    comment_content      text                NOT NULL,
    comment_approved     varchar(20)         NOT NULL DEFAULT '1',
    user_id              bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    comment_parent       bigint(20) UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (comment_ID)
) ENGINE=InnoDB;

CREATE TABLE wp_options (
    option_id    bigint(20) UNSIGNED NOT NULL AUTO_INCREMENT,
    option_name  varchar(191)        NOT NULL DEFAULT '',
    option_value longtext            NOT NULL,
    autoload     varchar(20)         NOT NULL DEFAULT 'yes',
    PRIMARY KEY (option_id)
) ENGINE=InnoDB;
"""

# WordPress uses no FK constraints in DDL — this is the enrichment a DBA adds.
WORDPRESS_ALTER_FKDL = """
ALTER TABLE wp_usermeta          ADD CONSTRAINT fk_um_user    FOREIGN KEY (user_id)          REFERENCES wp_users(ID);
ALTER TABLE wp_posts             ADD CONSTRAINT fk_p_author   FOREIGN KEY (post_author)       REFERENCES wp_users(ID);
ALTER TABLE wp_postmeta         ADD CONSTRAINT fk_pm_post    FOREIGN KEY (post_id)            REFERENCES wp_posts(ID);
ALTER TABLE wp_term_taxonomy    ADD CONSTRAINT fk_tt_term    FOREIGN KEY (term_id)            REFERENCES wp_terms(term_id);
ALTER TABLE wp_term_relationships ADD CONSTRAINT fk_tr_tt    FOREIGN KEY (term_taxonomy_id)   REFERENCES wp_term_taxonomy(term_taxonomy_id);
ALTER TABLE wp_term_relationships ADD CONSTRAINT fk_tr_post  FOREIGN KEY (object_id)          REFERENCES wp_posts(ID);
ALTER TABLE wp_comments         ADD CONSTRAINT fk_c_post     FOREIGN KEY (comment_post_ID)    REFERENCES wp_posts(ID);
ALTER TABLE wp_comments         ADD CONSTRAINT fk_c_user     FOREIGN KEY (user_id)            REFERENCES wp_users(ID);
"""

WORDPRESS_SQLITE = {
    "wp_users":             "ID INTEGER PRIMARY KEY, user_login TEXT, user_pass TEXT, user_nicename TEXT, user_email TEXT, user_registered TEXT, user_status INTEGER, display_name TEXT",
    "wp_usermeta":          "umeta_id INTEGER PRIMARY KEY, user_id INTEGER, meta_key TEXT, meta_value TEXT",
    "wp_posts":             "ID INTEGER PRIMARY KEY, post_author INTEGER, post_date TEXT, post_content TEXT, post_title TEXT, post_status TEXT, post_type TEXT, post_parent INTEGER, comment_count INTEGER",
    "wp_postmeta":          "meta_id INTEGER PRIMARY KEY, post_id INTEGER, meta_key TEXT, meta_value TEXT",
    "wp_terms":             "term_id INTEGER PRIMARY KEY, name TEXT, slug TEXT, term_group INTEGER",
    "wp_term_taxonomy":     "term_taxonomy_id INTEGER PRIMARY KEY, term_id INTEGER, taxonomy TEXT, description TEXT, parent INTEGER, count INTEGER",
    "wp_term_relationships":"object_id INTEGER, term_taxonomy_id INTEGER, term_order INTEGER, PRIMARY KEY(object_id, term_taxonomy_id)",
    "wp_comments":          "comment_ID INTEGER PRIMARY KEY, comment_post_ID INTEGER, comment_author TEXT, comment_author_email TEXT, comment_date TEXT, comment_content TEXT, comment_approved TEXT, user_id INTEGER, comment_parent INTEGER",
    "wp_options":           "option_id INTEGER PRIMARY KEY, option_name TEXT, option_value TEXT, autoload TEXT",
}

WORDPRESS_QUERIES = [
    ("Total posts",               "SELECT COUNT(*) FROM wp_posts WHERE post_type='post'"),
    ("Posts per author",          "SELECT u.display_name, COUNT(p.ID) FROM wp_posts p JOIN wp_users u ON p.post_author=u.ID GROUP BY u.ID ORDER BY 2 DESC LIMIT 5"),
    ("Usermeta per user (avg)",   "SELECT ROUND(AVG(c),1) FROM (SELECT COUNT(*) c FROM wp_usermeta GROUP BY user_id)"),
    ("Comments per post (avg)",   "SELECT ROUND(AVG(c),1) FROM (SELECT COUNT(*) c FROM wp_comments GROUP BY comment_post_ID)"),
    ("Terms by taxonomy",         "SELECT tt.taxonomy, COUNT(*) FROM wp_term_taxonomy tt GROUP BY tt.taxonomy ORDER BY 2 DESC LIMIT 5"),
    ("Posts with term (full join)","SELECT COUNT(DISTINCT p.ID) FROM wp_posts p JOIN wp_term_relationships tr ON p.ID=tr.object_id JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id=tt.term_taxonomy_id JOIN wp_terms t ON tt.term_id=t.term_id"),
    ("Post + comments count",     "SELECT COUNT(*) FROM wp_comments c JOIN wp_posts p ON c.comment_post_ID=p.ID"),
]


def test_wordpress_mysql_migration():
    """WordPress MySQL: BIGINT UNSIGNED PKs, no FK DDL (DBA-enriched), wp_ prefix tables."""
    from src.statschema.ddl_parser import parse_ddl
    from src.statschema.model import GenerationRule
    from src.statschema.schema_io import resolve_row_counts
    from src.statschema.schema_io import resolve_load_order

    # WordPress has no FK DDL; the DBA appends them to the canonical YAML.
    # We simulate that by concatenating a separate ALTER TABLE block.
    tables = parse_ddl(WORDPRESS_DDL + WORDPRESS_ALTER_FKDL, dialect="mysql")
    tbl_map = {t.name: t for t in tables}

    # Verify that the DBA-added FKs are detected
    total_fks = sum(len(t.fk_constraints or []) for t in tables)
    assert total_fks >= 7, f"Expected ≥7 FKs, got {total_fks}"

    tbl_map["wp_options"].row_count = 100
    for name, rpsf in [
        ("wp_users",500), ("wp_usermeta",3000), ("wp_posts",2000),
        ("wp_postmeta",4000), ("wp_terms",200), ("wp_term_taxonomy",220),
        ("wp_term_relationships",3000), ("wp_comments",1500),
    ]:
        tbl_map[name].row_count_per_sf = float(rpsf)

    def sg(tbl, col, **kw):
        c = next(x for x in tbl_map[tbl].columns if x.name == col)
        c.generation = GenerationRule(**kw)

    sg("wp_users",    "user_email",    format_pattern="email")
    sg("wp_users",    "user_login",    format_pattern="word")
    sg("wp_users",    "display_name",  format_pattern="full_name")
    sg("wp_posts",    "post_status",   values=["publish","draft","private","pending"])
    sg("wp_posts",    "post_type",     values=["post","page","attachment","revision"])
    sg("wp_term_taxonomy", "taxonomy", values=["category","post_tag","nav_menu"])
    sg("wp_options",  "option_name",   format_pattern="word")
    sg("wp_options",  "autoload",      values=["yes","no"])
    sg("wp_comments", "comment_approved", values=["1","0","spam"])

    counts  = resolve_row_counts(tables, scale_factor=0.1)
    ordered = resolve_load_order(tables)
    conn    = _load_into_sqlite(ordered, counts, WORDPRESS_SQLITE)
    _assert_queries(conn, WORDPRESS_QUERIES)
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Chinook  (PostgreSQL INT PKs + ALTER TABLE FKs)
#    Included here for CI coverage of the interactive session validation.
# ─────────────────────────────────────────────────────────────────────────────

CHINOOK_DDL = """
CREATE TABLE artist        (artist_id INT NOT NULL, name VARCHAR(120), CONSTRAINT artist_pkey PRIMARY KEY (artist_id));
CREATE TABLE album         (album_id INT NOT NULL, title VARCHAR(160) NOT NULL, artist_id INT NOT NULL, CONSTRAINT album_pkey PRIMARY KEY (album_id));
CREATE TABLE genre         (genre_id INT NOT NULL, name VARCHAR(120), CONSTRAINT genre_pkey PRIMARY KEY (genre_id));
CREATE TABLE media_type    (media_type_id INT NOT NULL, name VARCHAR(120), CONSTRAINT media_type_pkey PRIMARY KEY (media_type_id));
CREATE TABLE employee      (employee_id INT NOT NULL, last_name VARCHAR(20) NOT NULL, first_name VARCHAR(20) NOT NULL, title VARCHAR(30), reports_to INT, CONSTRAINT employee_pkey PRIMARY KEY (employee_id));
CREATE TABLE track         (track_id INT NOT NULL, name VARCHAR(200) NOT NULL, album_id INT, media_type_id INT NOT NULL, genre_id INT, milliseconds INT NOT NULL, unit_price NUMERIC(10,2) NOT NULL, CONSTRAINT track_pkey PRIMARY KEY (track_id));
CREATE TABLE playlist      (playlist_id INT NOT NULL, name VARCHAR(120), CONSTRAINT playlist_pkey PRIMARY KEY (playlist_id));
CREATE TABLE playlist_track(playlist_id INT NOT NULL, track_id INT NOT NULL, CONSTRAINT playlist_track_pkey PRIMARY KEY (playlist_id, track_id));
CREATE TABLE customer      (customer_id INT NOT NULL, first_name VARCHAR(40) NOT NULL, last_name VARCHAR(20) NOT NULL, email VARCHAR(60) NOT NULL, support_rep_id INT, country VARCHAR(40), CONSTRAINT customer_pkey PRIMARY KEY (customer_id));
CREATE TABLE invoice       (invoice_id INT NOT NULL, customer_id INT NOT NULL, invoice_date TIMESTAMP NOT NULL, total NUMERIC(10,2) NOT NULL, CONSTRAINT invoice_pkey PRIMARY KEY (invoice_id));
CREATE TABLE invoice_line  (invoice_line_id INT NOT NULL, invoice_id INT NOT NULL, track_id INT NOT NULL, unit_price NUMERIC(10,2) NOT NULL, quantity INT NOT NULL, CONSTRAINT invoice_line_pkey PRIMARY KEY (invoice_line_id));
ALTER TABLE album          ADD CONSTRAINT fk1  FOREIGN KEY (artist_id)      REFERENCES artist(artist_id);
ALTER TABLE employee       ADD CONSTRAINT fk2  FOREIGN KEY (reports_to)     REFERENCES employee(employee_id);
ALTER TABLE track          ADD CONSTRAINT fk3  FOREIGN KEY (album_id)       REFERENCES album(album_id);
ALTER TABLE track          ADD CONSTRAINT fk4  FOREIGN KEY (media_type_id)  REFERENCES media_type(media_type_id);
ALTER TABLE track          ADD CONSTRAINT fk5  FOREIGN KEY (genre_id)       REFERENCES genre(genre_id);
ALTER TABLE customer       ADD CONSTRAINT fk6  FOREIGN KEY (support_rep_id) REFERENCES employee(employee_id);
ALTER TABLE invoice        ADD CONSTRAINT fk7  FOREIGN KEY (customer_id)    REFERENCES customer(customer_id);
ALTER TABLE invoice_line   ADD CONSTRAINT fk8  FOREIGN KEY (invoice_id)     REFERENCES invoice(invoice_id);
ALTER TABLE invoice_line   ADD CONSTRAINT fk9  FOREIGN KEY (track_id)       REFERENCES track(track_id);
ALTER TABLE playlist_track ADD CONSTRAINT fk10 FOREIGN KEY (playlist_id)    REFERENCES playlist(playlist_id);
ALTER TABLE playlist_track ADD CONSTRAINT fk11 FOREIGN KEY (track_id)       REFERENCES track(track_id);
"""

CHINOOK_SQLITE = {
    "artist":         "artist_id INTEGER PRIMARY KEY, name TEXT",
    "album":          "album_id INTEGER PRIMARY KEY, title TEXT, artist_id INTEGER",
    "genre":          "genre_id INTEGER PRIMARY KEY, name TEXT",
    "media_type":     "media_type_id INTEGER PRIMARY KEY, name TEXT",
    "employee":       "employee_id INTEGER PRIMARY KEY, last_name TEXT, first_name TEXT, title TEXT, reports_to INTEGER",
    "track":          "track_id INTEGER PRIMARY KEY, name TEXT, album_id INTEGER, media_type_id INTEGER, genre_id INTEGER, milliseconds INTEGER, unit_price REAL",
    "playlist":       "playlist_id INTEGER PRIMARY KEY, name TEXT",
    "playlist_track": "playlist_id INTEGER, track_id INTEGER, PRIMARY KEY(playlist_id, track_id)",
    "customer":       "customer_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, support_rep_id INTEGER, country TEXT",
    "invoice":        "invoice_id INTEGER PRIMARY KEY, customer_id INTEGER, invoice_date TEXT, total REAL",
    "invoice_line":   "invoice_line_id INTEGER PRIMARY KEY, invoice_id INTEGER, track_id INTEGER, unit_price REAL, quantity INTEGER",
}

CHINOOK_QUERIES = [
    ("Total invoices",            "SELECT COUNT(*) FROM invoice"),
    ("Total revenue",             "SELECT ROUND(SUM(total),2) FROM invoice"),
    ("Revenue by country (top5)", "SELECT c.country, ROUND(SUM(i.total),2) FROM invoice i JOIN customer c ON i.customer_id=c.customer_id GROUP BY c.country ORDER BY 2 DESC LIMIT 5"),
    ("Top 5 customers by spend",  "SELECT c.first_name||' '||c.last_name, ROUND(SUM(i.total),2) FROM invoice i JOIN customer c ON i.customer_id=c.customer_id GROUP BY c.customer_id ORDER BY 2 DESC LIMIT 5"),
    ("Sales by genre (top 5)",    "SELECT g.name, COUNT(*) FROM invoice_line il JOIN track t ON il.track_id=t.track_id JOIN genre g ON t.genre_id=g.genre_id GROUP BY g.name ORDER BY 2 DESC LIMIT 5"),
    ("Avg tracks per album",      "SELECT ROUND(AVG(c),2) FROM (SELECT COUNT(*) c FROM track GROUP BY album_id)"),
    ("Full chain: il→track→artist","SELECT COUNT(*) FROM invoice_line il JOIN track t ON il.track_id=t.track_id JOIN album a ON t.album_id=a.album_id JOIN artist ar ON a.artist_id=ar.artist_id"),
]


def test_chinook_postgres_migration():
    """Chinook: PostgreSQL INT PKs, ALTER TABLE FKs, 11 tables, 4-hop JOIN."""
    from src.statschema.ddl_parser import parse_ddl
    from src.statschema.model import GenerationRule
    from src.statschema.schema_io import resolve_row_counts
    from src.statschema.schema_io import resolve_load_order

    tables = parse_ddl(CHINOOK_DDL, dialect="postgres")
    tbl_map = {t.name: t for t in tables}

    total_fks = sum(len(t.fk_constraints or []) for t in tables)
    assert total_fks == 11, f"Expected 11 FKs, got {total_fks}"

    tbl_map["genre"].row_count      = 25
    tbl_map["media_type"].row_count = 5
    tbl_map["employee"].row_count   = 8
    for name, rpsf in [("artist",275),("album",347),("track",3503),("playlist",18),
                       ("playlist_track",8715),("customer",59),("invoice",412),("invoice_line",2240)]:
        tbl_map[name].row_count_per_sf = float(rpsf)

    def sg(tbl, col, **kw):
        c = next(x for x in tbl_map[tbl].columns if x.name == col)
        c.generation = GenerationRule(**kw)

    sg("genre",      "name",         values=["Rock","Jazz","Metal","Pop","Blues","Latin","Reggae","Classical","Soundtrack","Alternative","Country","Electronic","Hip-Hop","R&B","World","Comedy","Drama","TV Shows","Sci-Fi","Bossa Nova","Heavy Metal","Easy Listening","Opera","Punk","Folk"])
    sg("media_type", "name",         values=["MPEG audio file","AAC audio file","Protected AAC audio file","MPEG-4 video","Purchased AAC"])
    sg("track",      "milliseconds", min_value=30000, max_value=900000)
    sg("track",      "unit_price",   values=[0.99, 1.99])
    sg("invoice_line","unit_price",  values=[0.99, 1.99])
    sg("invoice_line","quantity",    min_value=1, max_value=5)
    sg("invoice",    "total",        min_value=0.99, max_value=25.86)
    sg("customer",   "country",      values=["USA","Canada","Brazil","France","Germany","UK","Portugal","Netherlands","Australia","India"])
    sg("employee",   "title",        values=["General Manager","Sales Manager","Sales Support Agent","IT Manager","IT Staff"])
    sg("playlist",   "name",         values=["Music","Movies","TV Shows","Classical","Rock","90s Music","Grunge","Heavy Metal"])

    counts  = resolve_row_counts(tables, scale_factor=0.1)
    ordered = resolve_load_order(tables)
    conn    = _load_into_sqlite(ordered, counts, CHINOOK_SQLITE)
    _assert_queries(conn, CHINOOK_QUERIES)
    conn.close()
