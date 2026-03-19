# What are the key limitations to be aware of?

*Source: [Zerobus Ingest connector limitations](https://docs.databricks.com/aws/en/ingestion/zerobus-limits) (last updated March 4, 2026)*

| Limitation | Detail |
|---|---|
| **No private endpoint support** | Cannot write to storage secured through a private endpoint |
| **No catalog-managed commits** | Do not use Zerobus with tables that have catalog-managed commits enabled |
| **Managed tables only** | Only supports managed Delta tables; external/default storage tables are not supported |
| **No table auto-creation** | Target table must exist before ingestion |
| **No exactly-once delivery** | At-least-once only; deduplication must be handled by the consumer |
| **No schema auto-evolution** | Breaking schema changes reject records to the fallback location |
| **Max 2000 columns** | Per proto schema |
| **Max 10 MB per record** | Hard record size limit |
| **Partitioned tables** | No more than 1,000 partitions written within a 5-second interval |
| **ASCII table/column names only** | Letters, digits, and underscores only |
| **No table recreation** | Dropping and recreating a target table is not supported |
| **Transient buffer not covered by CMK** | Data in the serverless buffer before Delta materialization uses Databricks-managed encryption only |

---

*[← Back to FAQ index](../zerobus_faq.md)*
