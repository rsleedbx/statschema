# Does Zerobus support schema evolution?

**Partially.** Zerobus supports a limited, non-breaking form of schema evolution:

- **Adding nullable columns** to the target Delta table is supported. Missing fields in incoming records are filled with `NULL`.
- **Zerobus will never auto-evolve your target table** — you must apply schema changes in Unity Catalog yourself.
- **Breaking changes** (renaming columns, changing types, removing columns, changing nullability) will cause records to be rejected and routed to the durable fallback location (see [09-rejected-data.md](09-rejected-data.md)).

---

*[← Back to FAQ index](../zerobus_faq.md)*
