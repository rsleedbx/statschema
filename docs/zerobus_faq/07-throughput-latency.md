# What are the throughput and latency characteristics?

## Latency

*Source: [Zerobus Ingest connector limitations](https://docs.databricks.com/aws/en/ingestion/zerobus-limits) (last updated March 4, 2026)*

| Metric | P50 | P95 |
|---|---|---|
| Time to durability (ack) | ≤ 200 ms | ≤ 500 ms |
| Time to Delta table | ≤ 5 s | ≤ 30 s |

Actual latency varies based on region alignment and workload. Best performance is achieved when the client and the Zerobus endpoint are in the **same geographic region**.

## Throughput limits (defaults)

| Limit | Value |
|---|---|
| Records per second per stream | 15,000 |
| Throughput per stream | 100 MB/s (benchmarked at 1 KB messages) |
| Throughput per target table | 10 GB/s |
| Max record size | 10 MB |

To increase throughput, open **multiple parallel streams** to the same table. To raise the default quotas, contact your Databricks account representative.

---

*[← Back to FAQ index](../zerobus_faq.md)*
