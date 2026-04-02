"""SQL rewrite helpers shared by dialect adapters.

Each dialect's ``rewrite_query_sql`` method calls the functions it needs rather
than placing engine-specific logic inline in identity_test.py.
"""
from __future__ import annotations

import re

# Map identity-test dialect names → sqlglot read/write dialect tokens.
# "ansi" uses None so LIMIT / TIMESTAMP are parsed with the default grammar
# and then transpiled to the target dialect.
SQLGLOT_DIALECT: dict[str, str | None] = {
    "postgres":    "postgres",
    "neon":        "postgres",
    "cockroachdb": "postgres",
    "lakebase":    "postgres",
    "mysql":       "mysql",
    "mariadb":     "mysql",
    "sqlserver":   "tsql",
    "oracle":      "oracle",
    "db2":         "db2",
    "ansi":        None,
}


def strip_double_quotes(text: str) -> str:
    """Remove SQL double-quote identifier delimiters: \"foo\" → foo.

    Oracle stores unquoted identifiers as uppercase; stripping quotes lets
    Oracle auto-uppercase the names at parse time, matching catalog storage.
    """
    return re.sub(r'"([^"]+)"', r"\1", text)


def rewrite_limit_to_fetch(sql: str) -> str:
    """Rewrite LIMIT N → FETCH FIRST N ROWS ONLY (Oracle / DB2 syntax)."""
    return re.sub(
        r"\bLIMIT\s+(\d+)", r"FETCH FIRST \1 ROWS ONLY", sql, flags=re.IGNORECASE
    )


# Dialects that fold identifiers to uppercase (SQL-92 standard).
_UPPERCASE_FOLD_DIALECTS = frozenset({"oracle", "db2"})


def _normalize_name(name: str, target_sqlglot_dialect: str | None) -> str:
    """Return *name* in the form the target catalog stores it."""
    if target_sqlglot_dialect in _UPPERCASE_FOLD_DIALECTS:
        return name.upper()
    return name.lower()


def transpile_and_qualify(
    sql: str,
    schema_name: str,
    table_names: set[str],
    source_dialect: str,
    target_sqlglot_dialect: str | None,
) -> str:
    """Transpile *sql* to *target_sqlglot_dialect*, prefix unqualified table
    references with *schema_name*, and normalize identifier case to match how
    the target catalog stores names.

    Raises on parse or generation failure — callers must not silently swallow
    errors from this function.
    """
    import sqlglot
    import sqlglot.expressions as exp

    read_d = SQLGLOT_DIALECT.get(source_dialect)
    tree = sqlglot.parse_one(
        sql, read=read_d or None, error_level=sqlglot.ErrorLevel.WARN
    )

    tnames_lower = {t.lower() for t in table_names}
    for tbl in tree.find_all(exp.Table):
        if tbl.name.lower() in tnames_lower:
            normalized = _normalize_name(tbl.name, target_sqlglot_dialect)
            tbl.set("this", exp.Identifier(this=normalized, quoted=True))
            if not tbl.db:
                tbl.set("db", exp.Identifier(this=schema_name, quoted=True))

    # Normalize all quoted column identifiers to match catalog storage case.
    for col in tree.find_all(exp.Column):
        if col.this and col.this.quoted:
            col.set("this", exp.Identifier(
                this=_normalize_name(col.this.name, target_sqlglot_dialect),
                quoted=True,
            ))

    return tree.sql(dialect=target_sqlglot_dialect or None)
