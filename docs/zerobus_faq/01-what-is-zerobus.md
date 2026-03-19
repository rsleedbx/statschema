# What is Zerobus Ingest and how does it work?

Zerobus Ingest is a Databricks serverless, push-based ingestion API that writes data directly into Unity Catalog Delta tables without requiring message bus infrastructure (no Kafka, no brokers, no partition management).

**Data flow:**

```
Client app
    │
    │  gRPC or REST (Protobuf or JSON)
    ▼
Zerobus Ingest server (serverless, Databricks-managed)
    │  validates schema, buffers, makes durable, sends ack
    │
    ├──► (breaking schema change before publish?)
    │         │
    │         ▼
    │    <table_storage_root>/
    │    _zerobus/table_rejected_parquets/
    │    (Parquet files, within table storage boundary,
    │     same access controls & lifecycle as table)
    │
    ▼  (normal path)
Delta table in Unity Catalog (customer cloud storage)
```

**How it works:**

1. Your client opens a **stream** to the Zerobus endpoint for a given target table.
2. Records are pushed through the stream as Protobuf or JSON messages.
3. The Zerobus server validates each record against the target table schema.
4. Once durable, the server sends an **acknowledgment** back to the client.
5. The data is then **materialized** into the Delta table in your cloud storage.

Scaling is achieved by opening more parallel streams — there is no partition or broker configuration.

---

*[← Back to FAQ index](../zerobus_faq.md)*
