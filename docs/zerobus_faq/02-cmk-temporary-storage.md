# Does Zerobus store data in temporary storage where Customer-Managed Keys (CMK) apply?

**Yes, Zerobus uses a temporary buffer — and CMK coverage depends on which layer you are looking at.**

## How data moves through Zerobus

There are two distinct durability phases with measurably different latencies:

| Phase | Location | CMK applicable? |
|---|---|---|
| **Durable ack** (P50 ≤ 200 ms) | Zerobus buffer in **Databricks-managed serverless infrastructure** | **No** — Databricks encryption only |
| **Materialized in Delta table** (P50 ≤ 5 s) | Customer cloud storage (S3 / ADLS / GCS) | **Yes** — CMK applies if configured on the workspace |
| **Durable fallback** (`_zerobus/table_rejected_parquets/`) | Inside the table's storage root in **customer storage** | **Yes** — follows the same access controls as the table |

## Summary for compliance discussions

| Concern | Answer |
|---|---|
| Does Zerobus use temporary storage? | Yes — a serverless buffer before Delta materialization |
| Does CMK cover the transient buffer? | No |
| Does CMK cover the final Delta table data? | Yes (if CMK is configured on the workspace) |
| Does CMK cover rejected / fallback data? | Yes (stored within the table's storage boundary) |
| Private endpoint storage supported? | No (see [03-private-endpoint.md](03-private-endpoint.md)) |

If your compliance requirement is that **all data must be encrypted with CMK at all times** (including in-flight and in-buffer stages), contact your Databricks account team before using Zerobus.

---

*[← Back to FAQ index](../zerobus_faq.md)*
