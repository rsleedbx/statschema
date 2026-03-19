# Does Zerobus automatically create the target table?

**No.** Zerobus will never auto-create or auto-evolve your target table. You must:

1. Create the target Delta table in Unity Catalog before ingestion begins.
2. Ensure the table schema matches the records you intend to send.
3. Grant the service principal `USE_CATALOG`, `USE_SCHEMA`, `MODIFY`, and `SELECT` on the target table.

Zerobus will validate incoming records against the existing table schema and reject records that do not match.

---

*[← Back to FAQ index](../zerobus_faq.md)*
