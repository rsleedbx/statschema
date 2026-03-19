# How do I monitor Zerobus ingest?

## System tables

Two system tables track Zerobus activity:

| System table | What it tracks |
|---|---|
| `system.lakeflow.zerobus_stream` | Stream lifecycle events: open, close, errors |
| `system.lakeflow.zerobus_ingest` | Ingest batches: `commit_version`, `committed_bytes`, `committed_records` |

### Example: total records ingested for a table

```sql
SELECT SUM(ingest.committed_records) AS total_records
FROM system.lakeflow.zerobus_ingest AS ingest
WHERE ingest.table_name = 'catalog.schema.table_name'
  AND ingest.commit_time >= :start_timestamp
  AND ingest.commit_time <= :end_timestamp;
```

## Billing

Zerobus is billed under:
- **AWS**: `"Jobs Serverless"` SKU
- **Azure**: `"Automated Serverless"` SKU

Filter the billing system table with:

```sql
WHERE product_features.lakeflow_connect.zerobus_request_type IN ('GRPC', 'HTTP')
  AND billing_origin_product = 'LAKEFLOW_CONNECT'
```

*Source: [Zerobus Ingest system tables](https://docs.databricks.com/aws/en/admin/system-tables/zerobus-ingest)*

---

*[← Back to FAQ index](../zerobus_faq.md)*
