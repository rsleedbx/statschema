"""
Canonical schema model: independent of source format (YData YAML, pipeline YAML, DB dumps).
Used as the intermediate representation before converting to dbldatagen and for emitting
DDL to any target dialect (MySQL, PostgreSQL, SQL Server, Oracle, Databricks).

Design goals
------------
- Lossless round-trip: source DDL → canonical → target DDL preserves column types,
  precision, length, NOT NULL, DEFAULT, AUTO_INCREMENT, and PRIMARY KEY.
- General enough for all supported databases: precision/scale for DECIMAL, length for
  VARCHAR/CHAR/NVARCHAR, dialect-specific identity/sequence columns.
- Backward compatible: all new fields are Optional with safe defaults so existing callers
  that omit them continue to work.
"""

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass
class GenerationRule:
    """
    Data generation hints for a single column — consumed by the dbldatagen builder.

    Distribution control
    --------------------
    distribution
        How values are sampled.  One of:
          "auto"         Let the builder decide based on type and stats (default).
          "uniform"      Equal probability across the range [min_value, max_value].
          "normal"       Gaussian with mean / std from distribution_params.
          "zipf"         Power-law / Zipf; use for high-cardinality categoricals.
                         distribution_params: {"a": 1.5}  (exponent; 1.0 = classic Zipf)
          "exponential"  Exponential decay; use for inter-arrival times, ages.
                         distribution_params: {"scale": 1.0}  (1/λ)
          "constant"     All rows get the same value (set via ``values[0]``).
          "sequential"   Monotonically increasing integers/dates; use for PK columns.

    distribution_params
        Free-form dict of distribution-specific parameters, e.g.
        ``{"mean": 50.0, "std": 15.0}`` for "normal",
        ``{"a": 1.2}`` for "zipf".

    format_pattern
        Hint for realistic string value generation.  Either a named pattern or a
        Python-compatible regex.

        Named patterns:  "email"       user@domain.tld
                         "phone_us"    +1-NXX-NXX-XXXX
                         "phone_intl"  +CC-NNN-NNN-NNNN
                         "uuid"        xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
                         "ip_v4"       NNN.NNN.NNN.NNN
                         "ip_v6"       xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx
                         "url"         https://domain.tld/path
                         "postal_us"   NNNNN or NNNNN-NNNN
                         "postal_uk"   AN NAA / ANN NAA / AANN NAA
                         "ssn"         NNN-NN-NNNN
                         "credit_card" NNNN-NNNN-NNNN-NNNN
                         "iban"        CC2N NNNN NNNN NNNN …
                         "name_first"  Random given name drawn from locale word list
                         "name_last"   Random family name
                         "company"     Company name pattern
                         "address"     Street address
                         "city"        City name
                         "country_iso2" Two-letter ISO 3166-1 alpha-2 code
                         "currency_iso" Three-letter ISO 4217 code

        Regex fallback:  ``r"[A-Z]{2}\\d{6}"``  (e.g. a passport number)

    Injection flags (all default True — safe for testing)
    -------------------------------------------------------
    inject_boundary_values
        When True the generator guarantees at least one row with ``min_value``
        and one with ``max_value`` from ColumnStats (if available).  Essential
        for boundary / precision tests.
    inject_nulls_from_stats
        When True apply ``ColumnStats.null_fraction`` as the NULL probability.
        When False the column is generated fully populated (no NULLs).
    use_mcv_weights
        When True use ``ColumnStats.most_common_values`` frequencies as weights
        so that hot values appear at their real-world frequency (Zipf-like).
    inject_rare_events
        When True add a small number of rows with values from the tail of the
        distribution — values not in the top MCVs but in the histogram tail.
        Useful for testing rare-event handling and error paths.
    """

    # ── core range / value pool ───────────────────────────────────────────
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    values: Optional[list[Any]] = None
    weights: Optional[list[float]] = None
    max_length: Optional[int] = None
    unique: bool = False

    # ── distribution control ──────────────────────────────────────────────
    distribution: str = "auto"
    distribution_params: dict[str, Any] = field(default_factory=dict)

    # ── string format / pattern ───────────────────────────────────────────
    format_pattern: Optional[str] = None    # named pattern or regex

    # ── injection flags ───────────────────────────────────────────────────
    inject_boundary_values: bool = True
    inject_nulls_from_stats: bool = True
    use_mcv_weights: bool = True
    inject_rare_events: bool = False

    # ── escape hatch for generator-specific options ───────────────────────
    extra: dict[str, Any] = field(default_factory=dict)

    # ── YAML serialization ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.min_value is not None:        d["min_value"] = self.min_value
        if self.max_value is not None:        d["max_value"] = self.max_value
        if self.values is not None:           d["values"]    = self.values
        if self.weights is not None:          d["weights"]   = self.weights
        if self.max_length is not None:       d["max_length"] = self.max_length
        if self.unique:                       d["unique"]    = True
        if self.distribution != "auto":       d["distribution"] = self.distribution
        if self.distribution_params:          d["distribution_params"] = self.distribution_params
        if self.format_pattern is not None:   d["format_pattern"] = self.format_pattern
        if not self.inject_boundary_values:   d["inject_boundary_values"] = False
        if not self.inject_nulls_from_stats:  d["inject_nulls_from_stats"] = False
        if not self.use_mcv_weights:          d["use_mcv_weights"] = False
        if self.inject_rare_events:           d["inject_rare_events"] = True
        if self.extra:                        d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GenerationRule":
        return cls(
            min_value=d.get("min_value"),
            max_value=d.get("max_value"),
            values=d.get("values"),
            weights=d.get("weights"),
            max_length=d.get("max_length"),
            unique=bool(d.get("unique", False)),
            distribution=str(d.get("distribution", "auto")),
            distribution_params=dict(d.get("distribution_params") or {}),
            format_pattern=d.get("format_pattern"),
            inject_boundary_values=bool(d.get("inject_boundary_values", True)),
            inject_nulls_from_stats=bool(d.get("inject_nulls_from_stats", True)),
            use_mcv_weights=bool(d.get("use_mcv_weights", True)),
            inject_rare_events=bool(d.get("inject_rare_events", False)),
            extra=dict(d.get("extra") or {}),
        )


@dataclass
class CanonicalColumn:
    """
    Column definition in the canonical schema.

    Structural fields (used by DDL emitter)
    ----------------------------------------
    name          Column identifier.

    type          Normalised semantic type — one of:

                  Numeric
                    integer      INT / SMALLINT / TINYINT / MEDIUMINT
                    long         BIGINT
                    float        FLOAT / REAL (32-bit approximate)
                    double       DOUBLE / FLOAT(53) (64-bit approximate)
                    decimal      DECIMAL(p,s) / NUMERIC / NUMBER

                  String
                    string       VARCHAR / CHAR / TEXT / NVARCHAR / STRING

                  Boolean
                    boolean      BOOLEAN / BIT / TINYINT(1) / NUMBER(1)

                  Date / time
                    date         DATE (date only, no time component)
                    timestamp    DATETIME / DATETIME2 / TIMESTAMP (no timezone)
                    timestamptz  TIMESTAMP WITH TIME ZONE / DATETIMEOFFSET /
                                 MySQL TIMESTAMP (UTC storage)
                    time         TIME (time-of-day without timezone)
                    timetz       TIME WITH TIME ZONE / TIMETZ

                  Binary
                    binary       VARBINARY / BLOB / BYTEA / IMAGE / RAW / BINARY
                                 Fixed-length BINARY(n) and variable-length VARBINARY(n)
                                 both use `length` to record the declared byte length.
                                 None → unlimited (BLOB / BYTEA / LONGBLOB).

    length        Length for variable-length string types.
    precision     Total digit count for DECIMAL / NUMERIC.
    scale         Decimal places for DECIMAL / NUMERIC.
    fsp           Fractional-seconds precision for temporal types (0–9).
                    MySQL / PostgreSQL max: 6 (microseconds)
                    SQL Server max: 7 (100-nanosecond resolution)
                    Oracle max: 9 (nanoseconds)
                    Databricks: always 6, not specifiable in DDL
                  None → dialect default (usually 0 or max precision).
    not_null      Emit NOT NULL constraint.
    auto_increment  Emit AUTO_INCREMENT / SERIAL / IDENTITY /
                    GENERATED ALWAYS AS IDENTITY.
    default       Raw DEFAULT expression (e.g. ``'active'``, ``0``,
                  ``CURRENT_TIMESTAMP``).  None → no DEFAULT clause.
    primary_key   Column is (part of) the table's primary key.
    unique        Column has a UNIQUE constraint.

    Semantic / metadata fields
    --------------------------
    description   Human-readable column description.
    constraints   Extra source-specific metadata.
    generation    Data generation hints (ranges, value lists, uniqueness).
    references    Foreign key reference as (parent_table, parent_column).
    """

    # ── structural (DDL) ──────────────────────────────────────────────────────
    name: str
    type: str

    # Numeric precision / string length — preserved for lossless round-trip
    length: Optional[int] = None      # VARCHAR(N), CHAR(N), BINARY(N), VARBINARY(N), RAW(N)
    precision: Optional[int] = None   # DECIMAL(p,s), NUMERIC(p,s), NUMBER(p,s)
    scale: Optional[int] = None       # scale part of DECIMAL(p,s); None treated as 0
    fsp: Optional[int] = None         # fractional-seconds precision for temporal types
    unsigned: bool = False            # MySQL UNSIGNED modifier; may widen canonical type

    # Column-level constraints
    not_null: bool = False
    auto_increment: bool = False      # AUTO_INCREMENT / SERIAL / IDENTITY / GENERATED AS IDENTITY
    default: Optional[str] = None     # raw DEFAULT expression; None → no DEFAULT clause
    primary_key: bool = False
    unique: bool = False

    # ── metadata / generation ─────────────────────────────────────────────────
    description: Optional[str] = None
    constraints: Optional[dict[str, Any]] = None   # extra source-specific metadata
    generation: Optional[GenerationRule] = None
    references: Optional[tuple[str, str]] = None   # (parent_table, parent_column)

    # ── YAML serialization ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.length is not None:        d["length"]        = self.length
        if self.precision is not None:     d["precision"]     = self.precision
        if self.scale is not None:         d["scale"]         = self.scale
        if self.fsp is not None:           d["fsp"]           = self.fsp
        if self.unsigned:                  d["unsigned"]      = True
        if self.not_null:                  d["not_null"]      = True
        if self.auto_increment:            d["auto_increment"]= True
        if self.default is not None:       d["default"]       = self.default
        if self.primary_key:               d["primary_key"]   = True
        if self.unique:                    d["unique"]        = True
        if self.description is not None:   d["description"]   = self.description
        if self.constraints:               d["constraints"]   = self.constraints
        if self.generation is not None:    d["generation"]    = self.generation.to_dict()
        if self.references is not None:    d["references"]    = list(self.references)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalColumn":
        gen = GenerationRule.from_dict(d["generation"]) if d.get("generation") else None
        refs_raw = d.get("references")
        refs: Optional[tuple[str, str]] = tuple(refs_raw) if refs_raw else None  # type: ignore[assignment]
        return cls(
            name=str(d["name"]),
            type=str(d["type"]),
            length=d.get("length"),
            precision=d.get("precision"),
            scale=d.get("scale"),
            fsp=d.get("fsp"),
            unsigned=bool(d.get("unsigned", False)),
            not_null=bool(d.get("not_null", False)),
            auto_increment=bool(d.get("auto_increment", False)),
            default=d.get("default"),
            primary_key=bool(d.get("primary_key", False)),
            unique=bool(d.get("unique", False)),
            description=d.get("description"),
            constraints=d.get("constraints"),
            generation=gen,
            references=refs,
        )


@dataclass
class CanonicalForeignKey:
    """
    Foreign key constraint — supports single and composite FKs.

    Replaces the older ``CanonicalTableSchema.foreign_keys`` list-of-dicts and
    ``CanonicalColumn.references`` tuple; both are kept for backward compatibility.

    Examples
    --------
    Single-column FK:
        CanonicalForeignKey(columns=["customer_id"],
                            parent_table="customers", parent_columns=["id"])

    Composite FK:
        CanonicalForeignKey(columns=["order_id", "line_no"],
                            parent_table="order_lines",
                            parent_columns=["order_id", "line_no"])
    """

    columns: list[str]              # FK column(s) in this table
    parent_table: str               # referenced table name
    parent_columns: list[str]       # referenced column(s)
    name: Optional[str] = None      # constraint name (e.g. fk_orders_customer)
    parent_schema: Optional[str] = None   # schema of the parent table (cross-schema FKs)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "columns":        self.columns,
            "parent_table":   self.parent_table,
            "parent_columns": self.parent_columns,
        }
        if self.name:          d["name"]          = self.name
        if self.parent_schema: d["parent_schema"] = self.parent_schema
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalForeignKey":
        return cls(
            columns=list(d["columns"]),
            parent_table=str(d["parent_table"]),
            parent_columns=list(d["parent_columns"]),
            name=d.get("name"),
            parent_schema=d.get("parent_schema"),
        )


@dataclass
class CanonicalTableSchema:
    """
    Table in the canonical schema.

    Multi-instance / dedup support
    --------------------------------
    A single table definition can represent many physical tables that share the
    exact same schema — a common pattern in large applications (e.g. Mautic has
    hundreds of ``email_stats_N`` shards, all with the same column structure).

    ``aliases``
        Explicit list of additional table names that share this definition.
        ``expand_table_instances()`` emits one ``CanonicalTableSchema`` per alias
        (plus the base ``name``).  Useful when the logical names differ but the
        DDL is identical.

        Example (YAML)::

            - name: email_stats
              aliases:
                - email_stats_archive
                - email_stats_staging
              columns: [...]

        Produces tables: ``email_stats``, ``email_stats_archive``, ``email_stats_staging``.

    ``instance_count``
        When > 1 the base name is *replaced* by ``N`` numbered copies whose
        names are ``{name}{instance_suffix_format.format(i)}`` for
        ``i in 1 .. instance_count``.  Default is **1** (just the base name —
        backward compatible).

        Example (YAML)::

            - name: form_results
              instance_count: 1000
              instance_suffix_format: "_{:04d}"
              columns: [...]

        Produces: ``form_results_0001``, ``form_results_0002``, …, ``form_results_1000``.

    ``instance_suffix_format``
        Python format string applied to the instance number.  Must contain
        exactly one positional placeholder.  Default: ``"_{:04d}"`` →
        ``_0001``, ``_0002``, …

    Combining ``aliases`` and ``instance_count``
        Both can be specified together.  Numbered instances are generated first;
        aliases are appended as extra un-numbered copies:

        Example::

            name: events, instance_count: 3, aliases: [events_archive]
            → events_0001, events_0002, events_0003, events_archive

    Temporal ordering constraints
    --------------------------------
    Ordering rules between date/time columns that must hold in every generated row.
    Each entry is a simple expression: "<col_a> < <col_b>"  (or <=, >, >=).

    Examples
    --------
    - "end_date > start_date"         — event must end after it starts
    - "updated_at >= created_at"      — update cannot precede creation
    - "shipped_at > ordered_at"       — shipment after order
    - "birth_date < hire_date"        — employee born before hired

    The generator must validate / re-sample until all constraints are satisfied.
    Violations in synthetic data cause silent correctness bugs in query logic
    (e.g., date-range predicates or session-duration calculations).
    """

    name: str
    columns: list[CanonicalColumn]
    description: Optional[str] = None

    # Structured FK constraints (preferred; replaces the dict-list below)
    fk_constraints: Optional[list[CanonicalForeignKey]] = None

    # Legacy: table-level FK list (dict format from older parsers; kept for compat)
    foreign_keys: Optional[list[dict[str, str]]] = None

    # ── generation constraints ────────────────────────────────────────────
    temporal_ordering_constraints: list[str] = field(default_factory=list)

    # ── multi-instance / dedup ────────────────────────────────────────────
    aliases: list[str] = field(default_factory=list)
    instance_count: int = 1
    instance_suffix_format: str = "_{:04d}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name":    self.name,
            "columns": [c.to_dict() for c in self.columns],
        }
        if self.description:
            d["description"] = self.description
        if self.fk_constraints:
            d["fk_constraints"] = [fk.to_dict() for fk in self.fk_constraints]
        if self.foreign_keys:
            d["foreign_keys"] = self.foreign_keys
        if self.temporal_ordering_constraints:
            d["temporal_ordering_constraints"] = self.temporal_ordering_constraints
        if self.aliases:
            d["aliases"] = list(self.aliases)
        if self.instance_count != 1:
            d["instance_count"] = self.instance_count
        if self.instance_suffix_format != "_{:04d}":
            d["instance_suffix_format"] = self.instance_suffix_format
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalTableSchema":
        fks = [CanonicalForeignKey.from_dict(fk) for fk in d.get("fk_constraints", [])]
        return cls(
            name=str(d["name"]),
            columns=[CanonicalColumn.from_dict(c) for c in d.get("columns", [])],
            description=d.get("description"),
            fk_constraints=fks if fks else None,
            foreign_keys=d.get("foreign_keys"),
            temporal_ordering_constraints=list(d.get("temporal_ordering_constraints", [])),
            aliases=list(d.get("aliases") or []),
            instance_count=int(d.get("instance_count", 1)),
            instance_suffix_format=str(d.get("instance_suffix_format", "_{:04d}")),
        )


# ---------------------------------------------------------------------------
# Multi-instance expansion helper
# ---------------------------------------------------------------------------

def expand_table_instances(
    tables: Union[list["CanonicalTableSchema"], "CanonicalTableSchema"],
) -> list["CanonicalTableSchema"]:
    """
    Expand a list of canonical tables into a flat list, resolving
    ``aliases`` and ``instance_count`` into individual named tables.

    Rules
    -----
    - ``instance_count == 1`` and no ``aliases`` → table unchanged (backward compat).
    - ``instance_count > 1`` → the base name is *replaced* by N numbered copies:
      ``{name}{instance_suffix_format.format(i)}`` for ``i`` in ``1 .. instance_count``.
    - ``aliases`` → one extra copy per alias name is appended after the
      base / numbered copies.
    - Expanded copies share the same column definitions, FK constraints, and
      generation rules as the original.  FK *references* pointing to other
      tables are left unchanged (expand those tables separately if needed).

    Parameters
    ----------
    tables
        A single ``CanonicalTableSchema`` or a list of them.

    Returns
    -------
    Flat list of ``CanonicalTableSchema`` objects, each with a unique resolved
    ``name`` and with ``aliases``, ``instance_count``, and
    ``instance_suffix_format`` reset to defaults (to avoid re-expansion).

    Examples
    --------
    ::

        # 1000-shard pattern (Mautic-style)
        table = CanonicalTableSchema(
            name="email_stats", instance_count=1000, columns=[...]
        )
        tables = expand_table_instances(table)
        # → [CanonicalTableSchema(name="email_stats_0001"), ...,
        #    CanonicalTableSchema(name="email_stats_1000")]

        # Alias pattern (same schema, different logical names)
        table = CanonicalTableSchema(
            name="audit_log",
            aliases=["audit_log_archive", "audit_log_staging"],
            columns=[...],
        )
        tables = expand_table_instances(table)
        # → [CanonicalTableSchema(name="audit_log"),
        #    CanonicalTableSchema(name="audit_log_archive"),
        #    CanonicalTableSchema(name="audit_log_staging")]
    """
    if isinstance(tables, CanonicalTableSchema):
        tables = [tables]

    result: list[CanonicalTableSchema] = []
    for table in tables:
        result.extend(_expand_one(table))
    return result


def _expand_one(table: "CanonicalTableSchema") -> list["CanonicalTableSchema"]:
    """Return the expanded list of tables for a single definition."""
    names: list[str] = []

    if table.instance_count > 1:
        try:
            # Validate the format string with a dummy call before generating all names
            table.instance_suffix_format.format(1)
        except (IndexError, KeyError, ValueError) as exc:
            raise ValueError(
                f"Table '{table.name}': invalid instance_suffix_format "
                f"'{table.instance_suffix_format}': {exc}"
            ) from exc
        for i in range(1, table.instance_count + 1):
            names.append(f"{table.name}{table.instance_suffix_format.format(i)}")
    else:
        names.append(table.name)

    # Aliases are always appended (independent of instance_count)
    names.extend(table.aliases)

    if len(names) == 1 and names[0] == table.name:
        return [table]  # nothing to expand — return as-is

    return [
        dataclasses.replace(
            table,
            name=new_name,
            aliases=[],
            instance_count=1,
            instance_suffix_format="_{:04d}",
        )
        for new_name in names
    ]
