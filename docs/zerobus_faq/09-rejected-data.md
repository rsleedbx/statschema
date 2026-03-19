# What happens to data that Zerobus cannot write to the table?

If a breaking schema change is made to your target table after Zerobus has made data durable (acknowledged), but before it has been materialized into Delta, Zerobus writes the affected records to a **durable fallback location**:

```
<table_storage_root>/_zerobus/table_rejected_parquets/
```

Key properties of the fallback location:

- It is within the **table's physical storage boundary** — same bucket/container, same root path.
- It follows the **same access controls and lifecycle policies** as the table itself.
- CMK encryption applies here (since it is in customer storage).
- The files are standard **Parquet** format, so they can be read and re-ingested after fixing the schema.

*Source: [Zerobus Ingest connector overview — Durable Fallback Location](https://docs.databricks.com/aws/en/ingestion/zerobus-overview#durable-fallback-location)*

---

*[← Back to FAQ index](../zerobus_faq.md)*
