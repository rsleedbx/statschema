# Temporal Ordering Constraints

`temporal_ordering_constraints` is a table-level list of ordering rules
between date/time columns.  Each rule asserts that one timestamp must always
be before (or after) another in every generated row.

Without these constraints, synthetic data generators independently sample each
timestamp column, producing rows where `shipped_at < created_at` or
`updated_at < created_at` — logically impossible combinations that break
query logic relying on date ranges, session durations, or event ordering.

## Syntax

Each constraint is a string of the form:

```
<col_a>  <operator>  <col_b>
```

Supported operators: `<`, `<=`, `>`, `>=`

```yaml
temporal_ordering_constraints:
  - "shipped_at > created_at"
  - "updated_at >= created_at"
  - "end_date > start_date"
  - "birth_date < hire_date"
```

## Placement

Constraints are declared at the table level, not the column level:

```yaml
tables:
  - name: orders
    temporal_ordering_constraints:
      - "shipped_at > created_at"
      - "delivered_at >= shipped_at"
    columns:
      - name: id
        type: long
        primary_key: true
        not_null: true
        auto_increment: true
      - name: created_at
        type: timestamp
        not_null: true
      - name: shipped_at
        type: timestamp
      - name: delivered_at
        type: timestamp
```

## Common patterns

| Rule | Meaning |
|------|---------|
| `"shipped_at > ordered_at"` | Shipment happens after the order is placed |
| `"updated_at >= created_at"` | Last update cannot precede creation |
| `"end_date > start_date"` | Event ends after it starts |
| `"birth_date < hire_date"` | Employee born before being hired |
| `"resolved_at >= opened_at"` | Ticket resolved after being opened |
| `"expiry_date > issue_date"` | Document expires after it is issued |

## Why this matters for synthetic data

Date/time constraints are among the most common sources of silent correctness
bugs in synthetic test data:

- A `WHERE shipped_at BETWEEN created_at AND delivered_at` query returns wrong
  results if these three timestamps are independent random values.
- A session-duration calculation `DATEDIFF(logout_at, login_at)` produces
  negative numbers if `logout_at < login_at`.
- Data quality checks testing "no future dates" fail unpredictably.

The generator must re-sample until all constraints are satisfied for each row.

## Nullable temporal columns

When a constrained column is nullable (no `not_null: true`), the constraint
applies only to non-null pairs.  A `NULL` shipped_at does not violate
`"shipped_at > created_at"` — it simply means the item has not yet shipped.

## Multiple constraints on the same column

Constraints chain transitively.  If you declare:

```yaml
temporal_ordering_constraints:
  - "created_at < shipped_at"
  - "shipped_at < delivered_at"
```

The generator ensures `created_at < shipped_at < delivered_at` holds together
in every fully-populated row.

## Related

- [Data generation rules](06-generation-rules.md)
- [`src/statschema/model.py`](../../src/statschema/model.py) — `CanonicalTableSchema.temporal_ordering_constraints`
